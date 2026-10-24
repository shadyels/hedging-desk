//! d1-gateway-nats — see delta-one/CLAUDE.md for the crate's role and rules.
//!
//! NATS client (`async-nats`) plus the `rtrb` rings (ADR-013) connecting
//! it to the core thread: inbound `TargetPosition` -> `d1_core::Target`,
//! outbound `d1_core::ExecReport` -> `ExecutionReport`, and (ADR-014) marks,
//! position snapshots and Tier-1 limit alerts. Edge crate, off the
//! hot path -- mirrors `d1-gateway-fix`'s shape, `tokio`/`async` instead of a
//! blocking socket loop since that's the idiomatic `async-nats` API.

pub mod convert;
pub mod error;
pub mod limits;
pub mod marks;
pub mod positions;

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

use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use d1_analytics::TrackerRecord;
use d1_core::{BookId, CrossRecord, ExecReport, InstrumentId, Target, TransferRequest};
use futures_util::StreamExt;
use prost::Message as _;

pub use error::NatsError;
use limits::{AlertEvent, LimitBook, TierOneLimits};
use marks::MarkThrottle;
use pb::hedging::live::v1::{InternalTransferRequest, TargetPosition, ValuationSnapshot};
use positions::PositionMirror;

/// Poll/backoff interval for the outbound-exec drain and the inbound-target
/// subscribe timeout. Off the hot path (ADR-004): matches
/// `d1-gateway-fix::DRAIN_POLL_INTERVAL`'s reasoning exactly.
const DRAIN_POLL_INTERVAL: Duration = Duration::from_millis(10);
/// Cadence of the mark keepalive, the every-book position snapshots and the
/// limits staleness check (ADR-014 §2, §7, §3).
const PERIODIC_INTERVAL: Duration = Duration::from_secs(1);
/// Wildcard subscription for every book/instrument EXO publishes targets on
/// (`protocol/nats-subjects.md`: `exo.targets.<book>.<instrument>`).
const TARGET_SUBJECT_WILDCARD: &str = "exo.targets.>";
/// Wildcard subscription for every book EXO publishes directed transfers on
/// (`protocol/nats-subjects.md`: `exo.transfers.<book>`).
const TRANSFER_SUBJECT_WILDCARD: &str = "exo.transfers.*";
/// Wildcard subscription for every book EXO publishes valuations on
/// (`protocol/nats-subjects.md`: `exo.valuations.<book>`), feeding the Tier-1
/// limits (ADR-014 §3).
//
// ponytail: `d1.risk.firm` (`RiskSnapshot`) is not published -- the Tier-1
// limits read EXO's Greeks directly and nothing consumes a firm snapshot
// before the UI exists. Owner P3.M1 (ADR-014 "not done here").
const VALUATION_SUBJECT_WILDCARD: &str = "exo.valuations.*";

/// A mid for the gateway to publish as `MarkPrice` (ADR-014 §2). The core
/// pushes one per in-universe tick; the gateway decides what goes on the wire.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MarkUpdate {
    /// Instrument the mid is for.
    pub instrument: InstrumentId,
    /// D1's arrival mid, fixed-point x10^9, always `> 0`.
    pub mid_px_e9: i64,
    /// Feed clock: ns since feed-session start (NOT epoch) -- lineage only.
    pub feed_ts_ns: u64,
}

/// One absolute (book, instrument) position as the core sees it, mirrored to
/// the gateway for `PositionSnapshot` (ADR-014 §7). Absolute, so a later
/// update heals a dropped one.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PositionUpdate {
    /// Book.
    pub book: BookId,
    /// Instrument.
    pub instrument: InstrumentId,
    /// Net quantity, fixed-point x10^2.
    pub qty_e2: i64,
    /// Average cost, fixed-point x10^9 (0 when flat).
    pub avg_px_e9: i64,
    /// Signed unfilled quantity of open netting parent orders, x10^2.
    pub inflight_qty_e2: i64,
}

/// Everything `run_gateway` needs that is not a ring.
#[derive(Debug, Clone)]
pub struct GatewayConfig {
    /// NATS server URL.
    pub url: String,
    /// Every configured book: each gets a `PositionSnapshot` every second, and
    /// a valuation is accepted only for one of these (ADR-014 §3 rule 1).
    pub book_ids: Vec<BookId>,
    /// Every configured instrument: a valuation line is accepted only for one
    /// of these (ADR-014 §3 rule 3).
    pub instrument_ids: Vec<InstrumentId>,
    /// Tracker book -> its benchmark instrument.
    pub tracker_benchmarks: Vec<(BookId, InstrumentId)>,
    /// `d1.toml [tracker] sampling_interval_s`.
    pub sampling_interval_s: u32,
    /// `d1.toml [limits.defaults]`.
    pub limits: TierOneLimits,
    /// `d1.toml [limits] valuation_max_age_s`.
    pub valuation_max_age_s: u32,
}

/// The gateway's ends of the core's rings (ADR-013).
pub struct GatewayRings {
    /// Inbound `Target`s to the core.
    pub target_tx: rtrb::Producer<Target>,
    /// Outbound `ExecReport`s from the core.
    pub exec_rx: rtrb::Consumer<ExecReport>,
    /// Outbound `CrossRecord`s from the core.
    pub cross_rx: rtrb::Consumer<CrossRecord>,
    /// Inbound `TransferRequest`s to the core.
    pub transfer_tx: rtrb::Producer<TransferRequest>,
    /// Outbound `TrackerRecord`s from the core.
    pub tracker_rx: rtrb::Consumer<TrackerRecord>,
    /// Outbound `MarkUpdate`s from the core.
    pub mark_rx: rtrb::Consumer<MarkUpdate>,
    /// Outbound `PositionUpdate`s from the core.
    pub position_rx: rtrb::Consumer<PositionUpdate>,
}

/// Run the NATS gateway until `shutdown` is set: connects to `cfg.url`,
/// subscribes `exo.targets.>` -> convert -> push onto `target_tx`, and drains
/// `exec_rx` -> encode -> publish on `d1.exec.<book>.<instrument>`; also
/// marks, position snapshots and Tier-1 limit alerts (ADR-014). Blocks the
/// calling thread -- spawn it from `crates/d1`, same shape as
/// `d1_gateway_fix::run_initiator`.
///
/// ponytail: a NATS server unreachable at startup degrades rather than
/// crashes `d1` -- `async_nats::connect` fails fast by default (no
/// `retry_on_initial_connect`), so this logs and returns, leaving the
/// FIX-only path running. Upgrade to background reconnect/backoff once a
/// real deployment needs the NATS plane to recover without restarting `d1`.
pub fn run_gateway(
    cfg: GatewayConfig,
    rings: GatewayRings,
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
    runtime.block_on(run_gateway_async(cfg, rings, shutdown))
}

async fn run_gateway_async(
    cfg: GatewayConfig,
    rings: GatewayRings,
    shutdown: &AtomicBool,
) -> Result<(), NatsError> {
    let GatewayRings {
        mut target_tx,
        mut exec_rx,
        mut cross_rx,
        mut transfer_tx,
        mut tracker_rx,
        mut mark_rx,
        mut position_rx,
    } = rings;
    let client = match async_nats::connect(&cfg.url).await {
        Ok(client) => client,
        Err(err) => {
            eprintln!(
                "nats: connect to {} failed, running FIX-only: {err}",
                cfg.url
            );
            return Ok(());
        }
    };
    let mut target_subscriber = client.subscribe(TARGET_SUBJECT_WILDCARD).await?;
    let mut transfer_subscriber = client.subscribe(TRANSFER_SUBJECT_WILDCARD).await?;
    let mut valuation_subscriber = client.subscribe(VALUATION_SUBJECT_WILDCARD).await?;
    let mut seen_msg_ids = HashSet::new();
    // Resolved once at startup (core thread, `crates/d1/src/lib.rs::spawn`)
    // from each tracker book's `benchmark_symbol` -- a `HashMap` here is
    // just this gateway thread's own O(1) lookup by book, not a second
    // source of truth.
    let tracker_benchmarks: HashMap<BookId, InstrumentId> =
        cfg.tracker_benchmarks.iter().copied().collect();
    let mut limit_book = LimitBook::new(
        cfg.limits,
        Duration::from_secs(u64::from(cfg.valuation_max_age_s)),
        &cfg.book_ids,
        &cfg.instrument_ids,
    );
    let mut mark_throttle = MarkThrottle::new();
    let mut position_mirror = PositionMirror::new();
    let mut periodic = tokio::time::interval(PERIODIC_INTERVAL);
    periodic.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);

    while !shutdown.load(Ordering::Relaxed) {
        let mut periodic_due = false;
        tokio::select! {
            next = target_subscriber.next() => match next {
                Some(msg) => handle_target(&msg.payload, &mut seen_msg_ids, &mut target_tx),
                // The stream ending means the subscription is gone, not that
                // there's nothing to read. Silently falling through to the
                // sleep branch forever would leave `d1` looking healthy while
                // ignoring every EXO target for the rest of the process's
                // life. Resubscribe; if the client is truly dead, `?` exits
                // the gateway loudly and the FIX path keeps running.
                None => {
                    eprintln!("nats: target subscription ended, resubscribing to {TARGET_SUBJECT_WILDCARD}");
                    target_subscriber = client.subscribe(TARGET_SUBJECT_WILDCARD).await?;
                }
            },
            next = transfer_subscriber.next() => match next {
                Some(msg) => handle_transfer(&msg.payload, &mut seen_msg_ids, &mut transfer_tx),
                None => {
                    eprintln!("nats: transfer subscription ended, resubscribing to {TRANSFER_SUBJECT_WILDCARD}");
                    transfer_subscriber = client.subscribe(TRANSFER_SUBJECT_WILDCARD).await?;
                }
            },
            next = valuation_subscriber.next() => match next {
                Some(msg) => {
                    let events = handle_valuation(
                        msg.subject.as_str(),
                        &msg.payload,
                        &mut seen_msg_ids,
                        &mut limit_book,
                        Instant::now(),
                    );
                    publish_alerts(&client, &events).await;
                }
                None => {
                    eprintln!("nats: valuation subscription ended, resubscribing to {VALUATION_SUBJECT_WILDCARD}");
                    valuation_subscriber = client.subscribe(VALUATION_SUBJECT_WILDCARD).await?;
                }
            },
            _ = periodic.tick() => periodic_due = true,
            () = tokio::time::sleep(DRAIN_POLL_INTERVAL) => {}
        }

        // Drain to empty, not one per iteration: this loop is gated behind
        // the select above, so a single pop caps outbound reports at
        // ~1/DRAIN_POLL_INTERVAL and a fill burst overruns the core's ring.
        //
        // Drain order (ADR-014 §7): exec reports, crosses, tracker, marks,
        // positions -- so a position snapshot never races ahead of the
        // report that explains it within one iteration.
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

        while let Ok(record) = cross_rx.pop() {
            let msg_id = uuid::Uuid::now_v7().to_string();
            let pb_notice = convert::cross_record_to_pb(&record, msg_id, now_ns());
            if let Err(err) = client
                .publish(convert::CROSSES_SUBJECT, pb_notice.encode_to_vec().into())
                .await
            {
                eprintln!("nats: publish InternalCrossNotice failed, dropping: {err}");
            }
        }

        while let Ok(record) = tracker_rx.pop() {
            let msg_id = uuid::Uuid::now_v7().to_string();
            let benchmark = tracker_benchmarks.get(&record.book).copied();
            let subject = convert::tracker_subject(record.book);
            let pb_record = convert::tracker_record_to_pb(
                &record,
                benchmark,
                cfg.sampling_interval_s,
                msg_id,
                now_ns(),
            );
            if let Err(err) = client
                .publish(subject, pb_record.encode_to_vec().into())
                .await
            {
                eprintln!("nats: publish TrackerAnalytics failed, dropping: {err}");
            }
        }

        // Marks (ADR-014 §2). The keepalive republishes every ticked
        // instrument, so on a periodic iteration it replaces the change-driven
        // pass instead of duplicating it.
        while let Ok(update) = mark_rx.pop() {
            mark_throttle.on_update(update);
        }
        let now = Instant::now();
        let marks = if periodic_due {
            mark_throttle.keepalive(now)
        } else {
            mark_throttle.due(now)
        };
        for mark in &marks {
            let pb = convert::mark_to_pb(mark, uuid::Uuid::now_v7().to_string(), now_ns());
            publish_pb(
                &client,
                convert::marks_subject(mark.instrument),
                &pb,
                "MarkPrice",
            )
            .await;
        }

        // Positions (ADR-014 §7): every configured book each second (flat
        // books included), otherwise only books whose mirror changed,
        // coalesced to one snapshot per book per iteration.
        while let Ok(update) = position_rx.pop() {
            position_mirror.apply(update);
        }
        let books = if periodic_due {
            position_mirror.clear_dirty();
            cfg.book_ids.clone()
        } else {
            position_mirror.take_dirty()
        };
        for book in books {
            let pb = convert::positions_to_pb(
                book,
                &position_mirror.lines(book),
                uuid::Uuid::now_v7().to_string(),
                now_ns(),
            );
            publish_pb(
                &client,
                convert::positions_subject(book),
                &pb,
                "PositionSnapshot",
            )
            .await;
        }

        if periodic_due {
            let events = limit_book.tick(now);
            publish_alerts(&client, &events).await;
        }
    }

    eprintln!(
        "nats: valuation rejects at shutdown: {}",
        limit_book.reject_summary()
    );
    Ok(())
}

/// Encode and publish one message, log-and-drop on failure (same posture as
/// every other publish in this loop).
async fn publish_pb<M: prost::Message>(
    client: &async_nats::Client,
    subject: String,
    msg: &M,
    what: &str,
) {
    if let Err(err) = client.publish(subject, msg.encode_to_vec().into()).await {
        eprintln!("nats: publish {what} failed, dropping: {err}");
    }
}

/// Publish each limit level change as a `RiskLimitAlert` on `d1.alerts.limits`.
async fn publish_alerts(client: &async_nats::Client, events: &[AlertEvent]) {
    for event in events {
        println!("nats: limit alert {event:?}");
        let pb = convert::risk_limit_alert_to_pb(event, uuid::Uuid::now_v7().to_string(), now_ns());
        publish_pb(
            client,
            convert::ALERTS_SUBJECT.to_string(),
            &pb,
            "RiskLimitAlert",
        )
        .await;
    }
}

/// Decode one inbound `ValuationSnapshot`, dedupe it on `Meta.msg_id`, and
/// offer it to the limit aggregator (ADR-014 §3). Returns the alerts it
/// causes. A snapshot that fails decode or lacks a `msg_id` has no book to
/// hold responsible: counted and logged by `LimitBook`.
fn handle_valuation(
    subject: &str,
    payload: &[u8],
    seen_msg_ids: &mut HashSet<String>,
    limit_book: &mut LimitBook,
    now: Instant,
) -> Vec<AlertEvent> {
    let msg = match ValuationSnapshot::decode(payload) {
        Ok(msg) => msg,
        Err(err) => {
            limit_book.reject_unattributable(&format!("ValuationSnapshot decode failed: {err}"));
            return Vec::new();
        }
    };
    let msg_id = msg.meta.as_ref().map_or("", |meta| meta.msg_id.as_str());
    if !msg_id_ok(msg_id) {
        limit_book.reject_unattributable("ValuationSnapshot with missing or oversize Meta.msg_id");
        return Vec::new();
    }
    if !seen_msg_ids.insert(msg_id.to_string()) {
        println!(
            "nats: duplicate ValuationSnapshot msg_id={:?}, dropping",
            limits::truncated(msg_id)
        );
        return Vec::new();
    }
    limit_book.on_snapshot(
        convert::valuation_book_from_subject(subject),
        &convert::valuation_from_pb(&msg),
        now,
    )
}

/// Decode one inbound `TargetPosition`, dedupe it, and hand the core a plain
/// `Target`. Split out of the `select!` branch so the failure paths can bail
/// early without `continue` reaching into the macro's own loop.
///
/// ponytail: `seen_msg_ids` grows without bound, exactly like
/// `OrderStore::seen_execs` -- fine for a single session, needs an eviction
/// policy (or a JetStream-backed dedupe window) whenever long uptime does.
/// The set also dedupes `exo.valuations` (a fresh msg_id every 5 s per book);
/// bounding it first needs `transfer_id` business-key dedupe for transfers.
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
    if !msg_id_ok(msg_id) {
        eprintln!("nats: TargetPosition with missing or oversize Meta.msg_id, dropping");
        return;
    }
    if seen_msg_ids.contains(msg_id) {
        println!(
            "nats: duplicate TargetPosition msg_id={:?}, already applied, dropping",
            limits::truncated(msg_id)
        );
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
        eprintln!(
            "nats: target ring full, dropping TargetPosition msg_id={:?}",
            limits::truncated(msg_id)
        );
        return; // NOT marked seen: redelivery is how this one recovers.
    }
    seen_msg_ids.insert(msg_id.to_string());
}

/// Decode one inbound `InternalTransferRequest`, dedupe it, and hand the core
/// a plain `TransferRequest`. Near-copy of `handle_target` -- same dedupe
/// set (`Meta.msg_id` is a UUIDv7, globally unique regardless of message
/// type, so one shared set is enough) and the same don't-mark-seen-on-full-ring
/// recovery contract.
fn handle_transfer(
    payload: &[u8],
    seen_msg_ids: &mut HashSet<String>,
    transfer_tx: &mut rtrb::Producer<TransferRequest>,
) {
    let msg = match InternalTransferRequest::decode(payload) {
        Ok(msg) => msg,
        Err(err) => {
            eprintln!("nats: InternalTransferRequest decode failed, dropping: {err}");
            return;
        }
    };

    let msg_id = msg.meta.as_ref().map_or("", |meta| meta.msg_id.as_str());
    if !msg_id_ok(msg_id) {
        eprintln!("nats: InternalTransferRequest with missing or oversize Meta.msg_id, dropping");
        return;
    }
    if seen_msg_ids.contains(msg_id) {
        println!(
            "nats: duplicate InternalTransferRequest msg_id={:?}, already applied, dropping",
            limits::truncated(msg_id)
        );
        return;
    }

    let transfer = match convert::internal_transfer_to_transfer(&msg) {
        Ok(transfer) => transfer,
        Err(err) => {
            eprintln!("nats: bad InternalTransferRequest, dropping: {err}");
            return;
        }
    };

    if transfer_tx.push(transfer).is_err() {
        eprintln!(
            "nats: transfer ring full, dropping InternalTransferRequest msg_id={:?}",
            limits::truncated(msg_id)
        );
        return; // NOT marked seen: redelivery is how this one recovers.
    }
    seen_msg_ids.insert(msg_id.to_string());
}

/// Longest `Meta.msg_id` any handler accepts (a UUID is 36 bytes). The ids go
/// into the unbounded `seen_msg_ids` set, so an uncapped one lets a forged
/// 1 MB message pin 1 MB forever.
const MAX_MSG_ID_BYTES: usize = 64;

/// A `msg_id` is dedupable only if it is non-empty and at most 64 bytes.
/// Check BEFORE any `contains`/`insert` on `seen_msg_ids`.
fn msg_id_ok(msg_id: &str) -> bool {
    !msg_id.is_empty() && msg_id.len() <= MAX_MSG_ID_BYTES
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

    fn val_msg(book: u32, msg_id: &str, as_of_ns: u64, vega_e9: i64) -> Vec<u8> {
        use super::pb::hedging::common::v1::ValuationMeta;
        use super::pb::hedging::live::v1::valuation_snapshot::ProductLine;
        ValuationSnapshot {
            meta: if msg_id.is_empty() {
                None
            } else {
                Some(Meta {
                    msg_id: msg_id.to_string(),
                    producer: "exo".to_string(),
                    sent_ns: 1,
                    schema_version: 2,
                })
            },
            book_id: book,
            as_of_ns,
            valuation: Some(ValuationMeta {
                model_id: "m".into(),
                params_hash: "p".into(),
                git_sha: "g".into(),
                ..Default::default()
            }),
            lines: vec![ProductLine {
                underlying: Some(InstrumentRef {
                    instrument_id: 1001,
                    ..Default::default()
                }),
                vega_e9,
                ..Default::default()
            }],
            ..Default::default()
        }
        .encode_to_vec()
    }

    fn val_book() -> LimitBook {
        LimitBook::new(
            TierOneLimits {
                vega_warn_e9: 50,
                vega_hard_e9: 100,
                gamma_warn_qty_e2: 20,
                gamma_hard_qty_e2: 50,
                rho_warn_e9: 10,
                rho_hard_e9: 25,
            },
            Duration::from_secs(15),
            &[BookId(3), BookId(4)],
            &[InstrumentId(1001)],
        )
    }

    #[test]
    fn valuation_redelivery_is_deduped_and_alerts_once() {
        let mut seen = HashSet::new();
        let mut lb = val_book();
        let now = Instant::now();
        let p = val_msg(3, "v-1", 1, 120);
        let ev = handle_valuation("exo.valuations.3", &p, &mut seen, &mut lb, now);
        assert_eq!(ev.len(), 1);
        assert!(handle_valuation("exo.valuations.3", &p, &mut seen, &mut lb, now).is_empty());
        assert_eq!(lb.rejects(BookId(3)), 0);
    }

    #[test]
    fn valuation_undecodable_or_without_msg_id_is_unattributable() {
        let mut seen = HashSet::new();
        let mut lb = val_book();
        let now = Instant::now();
        assert!(
            handle_valuation("exo.valuations.3", &[0xff, 0xff], &mut seen, &mut lb, now).is_empty()
        );
        let no_id = val_msg(3, "", 1, 120);
        assert!(handle_valuation("exo.valuations.3", &no_id, &mut seen, &mut lb, now).is_empty());
        assert_eq!(lb.unattributable_rejects(), 2);
    }

    #[test]
    fn valuation_subject_payload_book_mismatch_raises_no_alert() {
        let mut seen = HashSet::new();
        let mut lb = val_book();
        let now = Instant::now();
        let p = val_msg(3, "v-1", 1, 500);
        assert!(handle_valuation("exo.valuations.4", &p, &mut seen, &mut lb, now).is_empty());
        assert!(
            handle_valuation(
                "exo.valuations.x",
                &val_msg(3, "v-2", 1, 500),
                &mut seen,
                &mut lb,
                now
            )
            .is_empty()
        );
        assert_eq!(lb.unattributable_rejects(), 2);
    }

    #[test]
    fn msg_id_cap_is_64_bytes_and_non_empty() {
        assert!(msg_id_ok(&"a".repeat(64)));
        assert!(!msg_id_ok(&"a".repeat(65)));
        assert!(!msg_id_ok(""));
    }

    #[test]
    fn oversize_msg_id_is_rejected_by_every_handler_and_never_stored() {
        let big = "x".repeat(65);
        let mut seen = HashSet::new();

        let (mut ttx, mut trx) = rtrb::RingBuffer::<Target>::new(4);
        handle_target(&target_msg(&big), &mut seen, &mut ttx);
        assert!(trx.pop().is_err());

        let (mut xtx, mut xrx) = rtrb::RingBuffer::<TransferRequest>::new(4);
        let transfer = InternalTransferRequest {
            meta: Some(Meta {
                msg_id: big.clone(),
                ..Default::default()
            }),
            instrument: Some(InstrumentRef {
                instrument_id: 1001,
                ..Default::default()
            }),
            from_book_id: 1,
            to_book_id: 2,
            qty_e2: 10,
            ..Default::default()
        }
        .encode_to_vec();
        handle_transfer(&transfer, &mut seen, &mut xtx);
        assert!(xrx.pop().is_err());

        let mut lb = val_book();
        let ev = handle_valuation(
            "exo.valuations.3",
            &val_msg(3, &big, 1, 500),
            &mut seen,
            &mut lb,
            Instant::now(),
        );
        assert!(ev.is_empty());
        assert_eq!(lb.unattributable_rejects(), 1);

        assert!(seen.is_empty(), "an oversize msg_id must never be stored");

        // The cap is inclusive: 64 bytes passes.
        handle_target(&target_msg(&"y".repeat(64)), &mut seen, &mut ttx);
        assert!(trx.pop().is_ok());
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
