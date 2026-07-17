//! d1-gateway-nats — see delta-one/CLAUDE.md for the crate's role and rules.
//!
//! NATS client (`async-nats`) plus the two `rtrb` rings (ADR-013) connecting
//! it to the core thread: inbound `TargetPosition` -> `d1_core::Target`,
//! outbound `d1_core::ExecReport` -> `ExecutionReport`. Edge crate, off the
//! hot path -- mirrors `d1-gateway-fix`'s shape, `tokio`/`async` instead of a
//! blocking socket loop since that's the idiomatic `async-nats` API.

pub mod convert;
pub mod error;

// prost emits cross-package references as `super::super::common::v1::Meta`
// (relative to each package's own module path), so the module nesting here
// must mirror the proto package paths (`hedging.common.v1`, `hedging.live.v1`)
// exactly, not just the flat output filenames.
#[allow(clippy::all, clippy::pedantic, clippy::nursery, missing_docs)]
pub mod pb {
    pub mod hedging {
        pub mod common {
            pub mod v1 {
                include!("gen/hedging.common.v1.rs");
            }
        }
        pub mod live {
            pub mod v1 {
                include!("gen/hedging.live.v1.rs");
            }
        }
    }
}

use std::collections::HashSet;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use d1_core::{ExecReport, Target};
use futures_util::StreamExt;
use prost::Message as _;

pub use error::NatsError;
use pb::hedging::live::v1::TargetPosition;

/// Poll/backoff interval for the outbound-exec drain and the inbound-target
/// subscribe timeout. Off the hot path (ADR-004): matches
/// `d1-gateway-fix::DRAIN_POLL_INTERVAL`'s reasoning exactly.
const DRAIN_POLL_INTERVAL: Duration = Duration::from_millis(10);
/// Wildcard subscription for every book/instrument EXO publishes targets on
/// (`protocol/nats-subjects.md`: `exo.targets.<book>.<instrument>`).
const TARGET_SUBJECT_WILDCARD: &str = "exo.targets.>";

/// Run the NATS gateway until `shutdown` is set: connects to `url`,
/// subscribes `exo.targets.>` -> convert -> push onto `target_tx`, and drains
/// `exec_rx` -> encode -> publish on `d1.exec.<book>.<instrument>`. Blocks the
/// calling thread -- spawn it from `crates/d1`, same shape as
/// `d1_gateway_fix::run_initiator`.
///
/// ponytail: a NATS server unreachable at startup degrades rather than
/// crashes `d1` -- `async_nats::connect` fails fast by default (no
/// `retry_on_initial_connect`), so this logs and returns, leaving the
/// FIX-only path running. Upgrade to background reconnect/backoff once a
/// real deployment needs the NATS plane to recover without restarting `d1`.
pub fn run_gateway(
    url: &str,
    target_tx: rtrb::Producer<Target>,
    exec_rx: rtrb::Consumer<ExecReport>,
    shutdown: &AtomicBool,
) -> Result<(), NatsError> {
    let runtime = match tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
    {
        Ok(runtime) => runtime,
        Err(err) => {
            eprintln!("nats: failed to build tokio runtime, running FIX-only: {err}");
            return Ok(());
        }
    };
    runtime.block_on(run_gateway_async(url, target_tx, exec_rx, shutdown))
}

async fn run_gateway_async(
    url: &str,
    mut target_tx: rtrb::Producer<Target>,
    mut exec_rx: rtrb::Consumer<ExecReport>,
    shutdown: &AtomicBool,
) -> Result<(), NatsError> {
    let client = match async_nats::connect(url).await {
        Ok(client) => client,
        Err(err) => {
            eprintln!("nats: connect to {url} failed, running FIX-only: {err}");
            return Ok(());
        }
    };
    let mut subscriber = client.subscribe(TARGET_SUBJECT_WILDCARD).await?;
    let mut seen_msg_ids = HashSet::new();

    while !shutdown.load(Ordering::Relaxed) {
        tokio::select! {
            next = subscriber.next() => match next {
                Some(msg) => handle_target(&msg.payload, &mut seen_msg_ids, &mut target_tx),
                // The stream ending means the subscription is gone, not that
                // there's nothing to read. Silently falling through to the
                // sleep branch forever would leave `d1` looking healthy while
                // ignoring every EXO target for the rest of the process's
                // life. Resubscribe; if the client is truly dead, `?` exits
                // the gateway loudly and the FIX path keeps running.
                None => {
                    eprintln!("nats: target subscription ended, resubscribing to {TARGET_SUBJECT_WILDCARD}");
                    subscriber = client.subscribe(TARGET_SUBJECT_WILDCARD).await?;
                }
            },
            () = tokio::time::sleep(DRAIN_POLL_INTERVAL) => {}
        }

        // Drain to empty, not one per iteration: this loop is gated behind
        // the select above, so a single pop caps outbound reports at
        // ~1/DRAIN_POLL_INTERVAL and a fill burst overruns the core's ring.
        while let Ok(report) = exec_rx.pop() {
            let subject = convert::exec_subject(report.book, report.instrument);
            let msg_id = uuid::Uuid::now_v7().to_string();
            let pb_report = convert::exec_report_to_pb(&report, msg_id, now_ns());
            if let Err(err) = client
                .publish(subject, pb_report.encode_to_vec().into())
                .await
            {
                eprintln!("nats: publish ExecutionReport failed, dropping: {err}");
            }
        }
    }

    Ok(())
}

/// Decode one inbound `TargetPosition`, dedupe it, and hand the core a plain
/// `Target`. Split out of the `select!` branch so the failure paths can bail
/// early without `continue` reaching into the macro's own loop.
///
/// ponytail: `seen_msg_ids` grows without bound, exactly like
/// `OrderStore::seen_execs` -- fine for a single session, needs an eviction
/// policy (or a JetStream-backed dedupe window) whenever long uptime does.
fn handle_target(
    payload: &[u8],
    seen_msg_ids: &mut HashSet<String>,
    target_tx: &mut rtrb::Producer<Target>,
) {
    let msg = match TargetPosition::decode(payload) {
        Ok(msg) => msg,
        Err(err) => {
            eprintln!("nats: TargetPosition decode failed, dropping: {err}");
            return;
        }
    };

    // Producers may redeliver, so consumers must dedupe (root CLAUDE.md
    // invariant #4) -- without this a redelivered target places a second
    // live order. The FIX side already does this on `ExecId`
    // (`OrderStore::seen_execs`); this is the same contract on the NATS side.
    // An absent or empty `msg_id` is undedupable, so it is rejected rather
    // than waved through.
    let msg_id = msg.meta.as_ref().map_or("", |meta| meta.msg_id.as_str());
    if msg_id.is_empty() {
        eprintln!("nats: TargetPosition without Meta.msg_id, dropping (cannot dedupe)");
        return;
    }
    if seen_msg_ids.contains(msg_id) {
        println!("nats: duplicate TargetPosition msg_id={msg_id}, already applied, dropping");
        return;
    }

    let target = match convert::target_position_to_target(&msg) {
        Ok(target) => target,
        Err(err) => {
            eprintln!("nats: bad TargetPosition, dropping: {err}");
            return;
        }
    };

    // ponytail: log-and-drop on a full ring, matching d1-gateway-fix's
    // inbound-exec drain -- a single demo-sized ring, not a backpressure
    // protocol yet.
    if target_tx.push(target).is_err() {
        eprintln!("nats: target ring full, dropping TargetPosition msg_id={msg_id}");
        return; // NOT marked seen: redelivery is how this one recovers.
    }
    seen_msg_ids.insert(msg_id.to_string());
}

/// Wall-clock send timestamp for `Meta.sent_ns` (protocol/CLAUDE.md). Off the
/// hot path -- a syscall here is exactly the edge-crate cost ADR-004 accepts.
fn now_ns() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_nanos() as u64)
}

#[cfg(test)]
#[allow(clippy::unwrap_used)] // tests: unwrap_used/expect_used are hot-path-only bans (delta-one/CLAUDE.md)
mod tests {
    use super::pb::hedging::{
        common::v1::{InstrumentRef, Meta},
        live::v1::TargetPosition,
    };
    use super::*;

    fn target_msg(msg_id: &str) -> Vec<u8> {
        TargetPosition {
            meta: Some(Meta {
                msg_id: msg_id.to_string(),
                producer: "exo".to_string(),
                sent_ns: 1,
                schema_version: 1,
            }),
            book_id: 1,
            instrument: Some(InstrumentRef {
                instrument_id: 1001,
                ..Default::default()
            }),
            target_qty_e2: 5_000,
            ..Default::default()
        }
        .encode_to_vec()
    }

    #[test]
    fn redelivered_msg_id_is_pushed_once() {
        let (tx, mut rx) = rtrb::RingBuffer::<Target>::new(4);
        let mut tx = tx;
        let mut seen = HashSet::new();
        let payload = target_msg("m-1");

        handle_target(&payload, &mut seen, &mut tx);
        handle_target(&payload, &mut seen, &mut tx); // producer redelivery

        assert_eq!(rx.pop().unwrap().target_qty_e2, 5_000);
        assert!(
            rx.pop().is_err(),
            "redelivery must not place a second target"
        );
    }

    #[test]
    fn distinct_msg_ids_both_pass() {
        let (tx, mut rx) = rtrb::RingBuffer::<Target>::new(4);
        let mut tx = tx;
        let mut seen = HashSet::new();

        handle_target(&target_msg("m-1"), &mut seen, &mut tx);
        handle_target(&target_msg("m-2"), &mut seen, &mut tx);

        assert!(rx.pop().is_ok());
        assert!(rx.pop().is_ok());
    }

    #[test]
    fn missing_msg_id_is_rejected() {
        let (tx, mut rx) = rtrb::RingBuffer::<Target>::new(4);
        let mut tx = tx;
        let mut seen = HashSet::new();

        let no_meta = TargetPosition {
            meta: None,
            book_id: 1,
            instrument: Some(InstrumentRef {
                instrument_id: 1001,
                ..Default::default()
            }),
            target_qty_e2: 5_000,
            ..Default::default()
        }
        .encode_to_vec();
        handle_target(&no_meta, &mut seen, &mut tx);

        assert!(rx.pop().is_err(), "undedupable target must not be applied");
    }

    #[test]
    fn full_ring_does_not_mark_msg_id_seen() {
        // A dropped target's redelivery is its only recovery path, so a
        // ring-full drop must stay retryable.
        let (tx, mut rx) = rtrb::RingBuffer::<Target>::new(1);
        let mut tx = tx;
        let mut seen = HashSet::new();

        handle_target(&target_msg("m-1"), &mut seen, &mut tx); // fills the ring
        handle_target(&target_msg("m-2"), &mut seen, &mut tx); // dropped: full
        assert!(!seen.contains("m-2"));

        let _ = rx.pop(); // drain, making room
        handle_target(&target_msg("m-2"), &mut seen, &mut tx); // redelivery lands
        assert!(
            rx.pop().is_ok(),
            "redelivery after a full-ring drop must apply"
        );
    }

    #[test]
    fn generated_types_construct_and_cross_reference() {
        let target = TargetPosition {
            meta: Some(Meta {
                msg_id: "abc".to_string(),
                producer: "exo".to_string(),
                sent_ns: 1,
                schema_version: 1,
            }),
            book_id: 7,
            ..Default::default()
        };
        assert_eq!(target.book_id, 7);
        assert_eq!(
            target.meta.as_ref().map(|m| m.producer.as_str()),
            Some("exo")
        );
    }
}
