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

    while !shutdown.load(Ordering::Relaxed) {
        tokio::select! {
            Some(msg) = subscriber.next() => {
                match TargetPosition::decode(msg.payload) {
                    Ok(target_msg) => match convert::target_position_to_target(&target_msg) {
                        // ponytail: log-and-drop on a full ring, matching
                        // d1-gateway-fix's inbound-exec drain -- a single
                        // demo-sized ring, not a backpressure protocol yet.
                        Ok(target) => if target_tx.push(target).is_err() {
                            eprintln!("nats: target ring full, dropping TargetPosition");
                        },
                        Err(err) => eprintln!("nats: bad TargetPosition, dropping: {err}"),
                    },
                    Err(err) => eprintln!("nats: TargetPosition decode failed, dropping: {err}"),
                }
            }
            () = tokio::time::sleep(DRAIN_POLL_INTERVAL) => {}
        }

        if let Ok(report) = exec_rx.pop() {
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

/// Wall-clock send timestamp for `Meta.sent_ns` (protocol/CLAUDE.md). Off the
/// hot path -- a syscall here is exactly the edge-crate cost ADR-004 accepts.
fn now_ns() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_nanos() as u64)
}

#[cfg(test)]
mod tests {
    use super::pb::hedging::{common::v1::Meta, live::v1::TargetPosition};

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
