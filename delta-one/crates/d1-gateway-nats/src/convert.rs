//! Edge converters between `d1-core` types and the NATS Protobuf wire
//! contract (`protocol/proto/`). Pure functions only — no client/session
//! state (that lives in `crate::lib`). ADR-004: proto never enters
//! `d1-core`; mirrors `d1-gateway-fix::convert`'s role for the FIX side.

use std::collections::{BTreeMap, BTreeSet};

use d1_analytics::TrackerRecord;
use d1_core::{
    BookId, ClOrdId, CrossRecord, CycleRef, ExecId, ExecReport, InstrumentId, NettingCycleId,
    OrderStatus, RunId, Side, Target, TransferRequest,
};

use crate::error::NatsError;
use crate::limits::{
    AlertEvent, FirmGreeks, Greek, Level, TierOneLimits, ValLine, Valuation, saturate_i64,
};
use crate::pb::hedging::common::v1::{InstrumentRef, Meta, Side as PbSide};
use crate::pb::hedging::live::v1::{
    ExecutionReport, HealthStatus, InternalCrossNotice, InternalTransferRequest, MarkPrice,
    OrdStatus, PositionSnapshot, RiskLimitAlert, RiskSnapshot, TargetPosition, TrackerAnalytics,
    ValuationSnapshot, position_snapshot, risk_limit_alert, risk_snapshot, tracker_analytics,
};
use crate::{MarkUpdate, PositionUpdate};

/// Subject `InternalCrossNotice` publishes to (`protocol/nats-subjects.md`:
/// `d1.crosses`).
pub const CROSSES_SUBJECT: &str = "d1.crosses";

/// Subject `RiskLimitAlert` publishes to (`protocol/nats-subjects.md`:
/// `d1.alerts.limits`).
pub const ALERTS_SUBJECT: &str = "d1.alerts.limits";

/// Subject `RiskSnapshot` publishes to (`protocol/nats-subjects.md`:
/// `d1.risk.firm`).
pub const RISK_FIRM_SUBJECT: &str = "d1.risk.firm";

/// Subject `HealthStatus` publishes to (`protocol/nats-subjects.md`:
/// `d1.health`).
pub const HEALTH_SUBJECT: &str = "d1.health";

/// Prefix of `exo.valuations.<book>`; the book token is numeric (ADR-014 §1).
const VALUATIONS_PREFIX: &str = "exo.valuations.";

/// Convert an inbound `TargetPosition` (subject `exo.targets.<book>.<instrument>`)
/// to the plain core `Target`. Errors if the required nested `instrument`
/// field is absent — unlike `book_id`/`target_qty_e2`, it has no zero value
/// that would silently mean something else.
pub fn target_position_to_target(msg: &TargetPosition) -> Result<Target, NatsError> {
    let instrument = msg
        .instrument
        .as_ref()
        .ok_or(NatsError::MissingField("instrument"))?;
    Ok(Target {
        book: BookId(msg.book_id),
        instrument: InstrumentId(instrument.instrument_id),
        target_qty_e2: msg.target_qty_e2,
        band_e2: msg.band_qty_e2,
    })
}

/// Subject an `ExecutionReport` for `book`/`instrument` publishes to
/// (`protocol/nats-subjects.md`: `d1.exec.<book>.<instrument>`).
#[must_use]
pub fn exec_subject(book: BookId, instrument: InstrumentId) -> String {
    format!("d1.exec.{}.{}", book.0, instrument.0)
}

/// Build the outbound `ExecutionReport` for a core `ExecReport`, stamping a
/// fresh `Meta` block (`protocol/CLAUDE.md`: every NATS payload carries one).
#[must_use]
pub fn exec_report_to_pb(
    report: &ExecReport,
    run: RunId,
    msg_id: String,
    sent_ns: u64,
) -> ExecutionReport {
    ExecutionReport {
        meta: Some(Meta {
            msg_id,
            producer: "delta-one".to_string(),
            sent_ns,
            schema_version: 2,
        }),
        cl_ord_id: clordid_to_string(&report.cl_ord_id),
        exec_id: execid_to_string(&report.exec_id),
        book_id: report.book.0,
        instrument: Some(InstrumentRef {
            instrument_id: report.instrument.0,
            ..Default::default()
        }),
        side: side_to_pb(report.side) as i32,
        status: status_to_pb(report.status) as i32,
        last_qty_e2: report.last_qty_e2,
        last_px_e9: report.last_px_e9,
        cum_qty_e2: report.cum_qty_e2,
        leaves_qty_e2: report.leaves_qty_e2,
        text: String::new(),
        allocation: report.allocation,
        book_cum_qty_e2: report.book_cum_qty_e2,
        // "" for a direct order (no minting cycle).
        netting_cycle_id: report.cycle_id.map_or_else(String::new, |n| {
            CycleRef {
                run,
                cycle: NettingCycleId::Cycle(n),
            }
            .to_string()
        }),
    }
}

/// Convert an inbound `InternalTransferRequest` (subject
/// `exo.transfers.<book>`) to the plain core `TransferRequest`. Errors if the
/// required nested `instrument` field is absent, mirroring
/// `target_position_to_target`.
pub fn internal_transfer_to_transfer(
    msg: &InternalTransferRequest,
) -> Result<TransferRequest, NatsError> {
    let instrument = msg
        .instrument
        .as_ref()
        .ok_or(NatsError::MissingField("instrument"))?;
    Ok(TransferRequest {
        instrument: InstrumentId(instrument.instrument_id),
        from_book: BookId(msg.from_book_id),
        to_book: BookId(msg.to_book_id),
        qty_e2: msg.qty_e2,
    })
}

/// Build the outbound `InternalCrossNotice` for a booked `CrossRecord`,
/// stamping a fresh `Meta` block (`protocol/CLAUDE.md`: every NATS payload
/// carries one). Mirrors `exec_report_to_pb`.
#[must_use]
pub fn cross_record_to_pb(
    record: &CrossRecord,
    run: RunId,
    msg_id: String,
    sent_ns: u64,
) -> InternalCrossNotice {
    InternalCrossNotice {
        meta: Some(Meta {
            msg_id,
            producer: "delta-one".to_string(),
            sent_ns,
            schema_version: 2,
        }),
        cross_id: record.cross_id.to_string(),
        instrument: Some(InstrumentRef {
            instrument_id: record.instrument.0,
            ..Default::default()
        }),
        buy_book_id: record.buy_book.0,
        sell_book_id: record.sell_book.0,
        qty_e2: record.qty_e2,
        ref_px_e9: record.ref_px_e9,
        px_policy_id: record.policy_id.to_string(),
        netting_cycle_id: CycleRef {
            run,
            cycle: record.netting_cycle_id,
        }
        .to_string(),
    }
}

/// Subject a `TrackerAnalytics` for `book` publishes to
/// (`protocol/nats-subjects.md`: `d1.tracker.<book>`), numeric token
/// matching `exec_subject`'s shape.
#[must_use]
pub fn tracker_subject(book: BookId) -> String {
    format!("d1.tracker.{}", book.0)
}

/// Build the outbound `TrackerAnalytics` for a core `d1_analytics::TrackerRecord`,
/// stamping a fresh `Meta` block (`protocol/CLAUDE.md`: every NATS payload
/// carries one). Mirrors `exec_report_to_pb`/`cross_record_to_pb`.
///
/// `benchmark` is the tracker book's benchmark instrument, resolved from its
/// `benchmark_symbol` (`d1_refdata::TrackerBook`) once at startup in the core
/// thread -- `TrackerRecord` itself stays id-based on `book` only (ADR-010's
/// pure-calculator boundary, `d1-analytics`'s own doc comment), so this
/// takes the resolved id as a separate parameter rather than growing that
/// struct. `None` when the benchmark symbol has no matching entry in the
/// instrument universe (populates only `instrument_id`, matching
/// `exec_report_to_pb`'s existing `InstrumentRef` population style).
///
/// P1.M5 is ex-post only (ADR-010 decision 1): `kind` is always
/// `KIND_EX_POST`. `sampling_interval_s` comes from `d1.toml [tracker]`
/// (`crates/d1/src/config.rs`) -- a TE sampled at 60s is not comparable to
/// one sampled daily, so a consumer cannot interpret `tracking_error_ann_e9`
/// without it.
#[must_use]
pub fn tracker_record_to_pb(
    record: &TrackerRecord,
    benchmark: Option<InstrumentId>,
    sampling_interval_s: u32,
    msg_id: String,
    sent_ns: u64,
) -> TrackerAnalytics {
    TrackerAnalytics {
        meta: Some(Meta {
            msg_id,
            producer: "delta-one".to_string(),
            sent_ns,
            schema_version: 1,
        }),
        book_id: record.book.0,
        benchmark: benchmark.map(|id| InstrumentRef {
            instrument_id: id.0,
            ..Default::default()
        }),
        kind: tracker_analytics::Kind::ExPost as i32,
        tracking_error_ann_e9: record.tracking_error_ann_e9,
        tracking_diff_e9: record.tracking_diff_e9,
        cash_weight_e9: record.cash_weight_e9,
        cash_drag_e9: record.cash_drag_e9,
        n_obs: record.n_obs,
        window_start_ns: record.window_start_ns,
        window_end_ns: record.window_end_ns,
        sampling_interval_s,
    }
}

/// Subject a `MarkPrice` publishes to (`d1.marks.<instrument>`, numeric token,
/// ADR-014 §1/§2).
#[must_use]
pub fn marks_subject(instrument: InstrumentId) -> String {
    format!("d1.marks.{}", instrument.0)
}

/// Subject a `PositionSnapshot` publishes to (`d1.positions.<book>`, numeric
/// token, ADR-014 §1/§7).
#[must_use]
pub fn positions_subject(book: BookId) -> String {
    format!("d1.positions.{}", book.0)
}

/// Build the outbound `MarkPrice` (ADR-014 §2). `feed_ts_ns` is the D1 feed
/// clock (ns since feed-session start), NOT epoch; ordering uses `Meta.sent_ns`.
#[must_use]
pub fn mark_to_pb(mark: &MarkUpdate, msg_id: String, sent_ns: u64) -> MarkPrice {
    MarkPrice {
        meta: Some(meta(msg_id, sent_ns, 1)),
        instrument: Some(InstrumentRef {
            instrument_id: mark.instrument.0,
            ..Default::default()
        }),
        mid_px_e9: mark.mid_px_e9,
        feed_ts_ns: mark.feed_ts_ns,
    }
}

/// Build the outbound `PositionSnapshot` for `book` from its non-flat lines
/// (ADR-014 §7; a missing line means 0, an empty `lines` is a flat book).
#[must_use]
pub fn positions_to_pb(
    book: BookId,
    lines: &[PositionUpdate],
    msg_id: String,
    sent_ns: u64,
) -> PositionSnapshot {
    PositionSnapshot {
        meta: Some(meta(msg_id, sent_ns, 1)),
        book_id: book.0,
        lines: lines
            .iter()
            .map(|l| position_snapshot::Line {
                instrument: Some(InstrumentRef {
                    instrument_id: l.instrument.0,
                    ..Default::default()
                }),
                qty_e2: l.qty_e2,
                avg_px_e9: l.avg_px_e9,
                inflight_qty_e2: l.inflight_qty_e2,
            })
            .collect(),
    }
}

/// Build the outbound `RiskLimitAlert` for a level change (ADR-014 §3/§4).
/// Gamma's unit is `qty_e2`, so it fills the `_qty_e2` fields and leaves
/// `value_e9`/`limit_e9` at 0; vega and rho do the reverse.
#[must_use]
pub fn risk_limit_alert_to_pb(event: &AlertEvent, msg_id: String, sent_ns: u64) -> RiskLimitAlert {
    let (value_e9, limit_e9, value_qty_e2, limit_qty_e2) = match event.greek {
        Greek::Gamma => (0, 0, event.value, event.limit),
        Greek::Vega | Greek::Rho => (event.value, event.limit, 0, 0),
    };
    RiskLimitAlert {
        meta: Some(meta(msg_id, sent_ns, 2)),
        underlying: Some(InstrumentRef {
            instrument_id: event.underlying.0,
            ..Default::default()
        }),
        greek: match event.greek {
            Greek::Vega => risk_limit_alert::Greek::Vega,
            Greek::Gamma => risk_limit_alert::Greek::Gamma,
            Greek::Rho => risk_limit_alert::Greek::Rho,
        } as i32,
        value_e9,
        limit_e9,
        breached: event.level == Level::Hard,
        value_qty_e2,
        limit_qty_e2,
        text: event.text.clone(),
        cleared: event.level == Level::Ok,
    }
}

fn level_to_pb(level: Level) -> i32 {
    let pb = match level {
        Level::Ok => risk_snapshot::LimitLevel::Ok,
        Level::Warn => risk_snapshot::LimitLevel::Warn,
        Level::Hard => risk_snapshot::LimitLevel::Hard,
    };
    pb as i32
}

/// Build the firm `RiskSnapshot` (ADR-014 Amendment 1). One row per
/// underlying that has an accepted EXO line, a non-OK level, or a non-zero net
/// D1 position; an empty input still encodes (zero rows). `exo` is
/// `LimitBook::firm_greeks`, `d1_qty` is `PositionMirror::net_qty_by_instrument`,
/// `mid` is `MarkThrottle::latest_mid`.
///
/// `firm_delta_qty_e2` = Σ D1 position + Σ EXO delta (inflight excluded).
/// `notional_e9` = firm delta x mid / 100 in `i128`, 0 without a mark. Every
/// sum saturates to `i64` by sign. Thresholds come from `limits`, never from
/// constants here.
///
/// ponytail: (N1) (a) no `underlying_id` roll-up of options/futures onto their
/// underlying -- refdata carries no multiplier; (b) OPTION-class D1 positions
/// count at delta = qty (owner P4.M3, the first D1 option position); (c) the
/// tracker book's liability delta is not represented (owner undecided;
/// trigger: the first tracker liability model).
#[must_use]
pub fn risk_snapshot_to_pb(
    exo: &BTreeMap<InstrumentId, FirmGreeks>,
    d1_qty: &BTreeMap<InstrumentId, i128>,
    mid: impl Fn(InstrumentId) -> Option<i64>,
    limits: &TierOneLimits,
    msg_id: String,
    sent_ns: u64,
) -> RiskSnapshot {
    let ids: BTreeSet<InstrumentId> = exo
        .keys()
        .copied()
        .chain(d1_qty.iter().filter(|(_, q)| **q != 0).map(|(id, _)| *id))
        .collect();
    let greeks = ids
        .into_iter()
        .map(|id| {
            let g = exo.get(&id);
            let d1 = d1_qty.get(&id).copied().unwrap_or(0);
            let firm_delta = d1.saturating_add(g.map_or(0, |g| g.exo_delta_qty_e2));
            let notional = mid(id).map_or(0, |m| firm_delta.saturating_mul(i128::from(m)) / 100);
            let lvl = |f: fn(&FirmGreeks) -> Level| g.map_or(Level::Ok, f);
            let sum = |f: fn(&FirmGreeks) -> i128| saturate_i64(g.map_or(0, f));
            risk_snapshot::NetGreeks {
                underlying: Some(InstrumentRef {
                    instrument_id: id.0,
                    ..Default::default()
                }),
                firm_delta_qty_e2: saturate_i64(firm_delta),
                notional_e9: saturate_i64(notional),
                vega_e9: sum(|g| g.vega_e9),
                gamma_qty_e2: sum(|g| g.gamma_qty_e2),
                greeks_flagged: g.is_some_and(|g| g.flagged),
                rho_e9: sum(|g| g.rho_e9),
                theta_e9: sum(|g| g.theta_e9),
                div_sens_e9: sum(|g| g.div_sens_e9),
                vega_level: level_to_pb(lvl(|g| g.vega)),
                vega_warn_e9: limits.vega_warn_e9,
                vega_hard_e9: limits.vega_hard_e9,
                gamma_level: level_to_pb(lvl(|g| g.gamma)),
                gamma_warn_qty_e2: limits.gamma_warn_qty_e2,
                gamma_hard_qty_e2: limits.gamma_hard_qty_e2,
                rho_level: level_to_pb(lvl(|g| g.rho)),
                rho_warn_e9: limits.rho_warn_e9,
                rho_hard_e9: limits.rho_hard_e9,
                limit_data_suspect: g.is_some_and(|g| g.suspect),
            }
        })
        .collect();
    RiskSnapshot {
        meta: Some(meta(msg_id, sent_ns, 2)),
        greeks,
    }
}

/// Book token of an `exo.valuations.<book>` subject; `None` if the subject is
/// not of that shape or the token is not the CANONICAL decimal `u32` (ADR-014
/// §1): `+3` and `03` parse as 3 but are not what any publisher emits.
#[must_use]
pub fn valuation_book_from_subject(subject: &str) -> Option<BookId> {
    let token = subject.strip_prefix(VALUATIONS_PREFIX)?;
    let id: u32 = token.parse().ok()?;
    (id.to_string() == token).then_some(BookId(id))
}

/// Reduce an inbound `ValuationSnapshot` to what the Tier-1 limits read
/// (`limits::LimitBook`). Infallible: anything the acceptance rules must
/// reject (absent `valuation`, line without `underlying`, oversize lists) is
/// carried through as data for `LimitBook::on_snapshot` to judge.
#[must_use]
pub fn valuation_from_pb(msg: &ValuationSnapshot) -> Valuation {
    Valuation {
        book: BookId(msg.book_id),
        as_of_ns: msg.as_of_ns,
        meta_complete: msg.valuation.as_ref().is_some_and(|v| {
            !v.model_id.is_empty() && !v.params_hash.is_empty() && !v.git_sha.is_empty()
        }),
        lines: msg
            .lines
            .iter()
            .map(|l| ValLine {
                underlying: l.underlying.as_ref().map(|u| InstrumentId(u.instrument_id)),
                vega_e9: l.vega_e9,
                gamma_qty_e2: l.gamma_qty_e2,
                rho_e9: l.rho_e9,
                delta_qty_e2: l.delta_qty_e2,
                theta_e9: l.theta_e9,
                div_sens_e9: l.div_sens_e9,
                n_flagged: l.flagged_greeks.len(),
            })
            .collect(),
    }
}

/// Build the outbound `HealthStatus` (`d1.health`). `detail` must hold only
/// fixed tokens and decimal integers (`health::health_detail`), never a string
/// taken from a payload (ADR-014 §14).
#[must_use]
pub fn health_status_to_pb(
    ok: bool,
    detail: BTreeMap<String, String>,
    msg_id: String,
    sent_ns: u64,
) -> HealthStatus {
    HealthStatus {
        meta: Some(meta(msg_id, sent_ns, 1)),
        component: "delta-one".to_string(),
        ok,
        detail: detail.into_iter().collect(),
    }
}

/// `Meta` with this message type's `schema_version` (per type, not global:
/// 2 for `RiskLimitAlert`, whose fields 7-10 were added in P2.M4, ADR-014 §4,
/// and for `RiskSnapshot`, whose fields 10-19 are ADR-014 Amendment 1).
fn meta(msg_id: String, sent_ns: u64, schema_version: u32) -> Meta {
    Meta {
        msg_id,
        producer: "delta-one".to_string(),
        sent_ns,
        schema_version,
    }
}

/// `d1-core` ids are fixed 20-byte ASCII arrays (zero-padded decimal, see
/// `ids.rs`); the proto fields are plain strings with no length cap, so a
/// lossy decode is exact for every id this system ever mints and never
/// needs the FIX side's reject-on-invalid-UTF8 handling.
fn clordid_to_string(id: &ClOrdId) -> String {
    String::from_utf8_lossy(&id.0).into_owned()
}

fn execid_to_string(id: &ExecId) -> String {
    String::from_utf8_lossy(id.unpadded()).into_owned()
}

fn side_to_pb(side: Side) -> PbSide {
    match side {
        Side::Buy => PbSide::Buy,
        Side::Sell => PbSide::Sell,
    }
}

fn status_to_pb(status: OrderStatus) -> OrdStatus {
    match status {
        OrderStatus::New => OrdStatus::New,
        OrderStatus::PartiallyFilled => OrdStatus::PartiallyFilled,
        OrderStatus::Filled => OrdStatus::Filled,
        OrderStatus::Rejected => OrdStatus::Rejected,
        OrderStatus::Canceled => OrdStatus::Canceled,
        OrderStatus::PendingCancel => OrdStatus::PendingCancel,
        OrderStatus::PendingReplace => OrdStatus::PendingReplace,
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)] // tests: unwrap_used/expect_used are hot-path-only bans (delta-one/CLAUDE.md)
mod tests {
    use super::*;

    #[test]
    fn target_position_to_target_reads_required_fields() {
        let msg = TargetPosition {
            book_id: 1,
            instrument: Some(InstrumentRef {
                instrument_id: 1001,
                ..Default::default()
            }),
            target_qty_e2: -12_345,
            band_qty_e2: 500,
            ..Default::default()
        };
        let target = target_position_to_target(&msg).unwrap();
        assert_eq!(target.book, BookId(1));
        assert_eq!(target.instrument, InstrumentId(1001));
        assert_eq!(target.target_qty_e2, -12_345);
        assert_eq!(target.band_e2, 500);
    }

    #[test]
    fn target_position_to_target_missing_instrument_errors() {
        let msg = TargetPosition {
            book_id: 1,
            instrument: None,
            ..Default::default()
        };
        assert!(matches!(
            target_position_to_target(&msg),
            Err(NatsError::MissingField("instrument"))
        ));
    }

    #[test]
    fn internal_transfer_to_transfer_reads_required_fields() {
        let msg = InternalTransferRequest {
            instrument: Some(InstrumentRef {
                instrument_id: 1001,
                ..Default::default()
            }),
            from_book_id: 1,
            to_book_id: 5,
            qty_e2: 40_000,
            ..Default::default()
        };
        let transfer = internal_transfer_to_transfer(&msg).unwrap();
        assert_eq!(transfer.instrument, InstrumentId(1001));
        assert_eq!(transfer.from_book, BookId(1));
        assert_eq!(transfer.to_book, BookId(5));
        assert_eq!(transfer.qty_e2, 40_000);
    }

    #[test]
    fn internal_transfer_to_transfer_missing_instrument_errors() {
        let msg = InternalTransferRequest {
            instrument: None,
            ..Default::default()
        };
        assert!(matches!(
            internal_transfer_to_transfer(&msg),
            Err(NatsError::MissingField("instrument"))
        ));
    }

    #[test]
    fn cross_record_to_pb_maps_all_fields() {
        let record = CrossRecord {
            cross_id: uuid::Uuid::nil(),
            instrument: InstrumentId(1001),
            buy_book: BookId(1),
            sell_book: BookId(2),
            qty_e2: 800_000,
            ref_px_e9: 150_000_000_000,
            policy_id: "ARRIVAL_MID",
            netting_cycle_id: d1_core::NettingCycleId::Cycle(2),
        };
        let run = RunId(uuid::Uuid::from_u128(0xabc));
        let pb = cross_record_to_pb(&record, run, "msg-1".to_string(), 42);
        assert_eq!(pb.meta.as_ref().unwrap().schema_version, 2);
        assert_eq!(
            pb.netting_cycle_id,
            "00000000-0000-0000-0000-000000000abc/2"
        );

        assert_eq!(pb.meta.as_ref().unwrap().msg_id, "msg-1");
        assert_eq!(pb.meta.as_ref().unwrap().producer, "delta-one");
        assert_eq!(pb.meta.as_ref().unwrap().sent_ns, 42);
        assert_eq!(pb.cross_id, uuid::Uuid::nil().to_string());
        assert_eq!(pb.instrument.as_ref().unwrap().instrument_id, 1001);
        assert_eq!(pb.buy_book_id, 1);
        assert_eq!(pb.sell_book_id, 2);
        assert_eq!(pb.qty_e2, 800_000);
        assert_eq!(pb.ref_px_e9, 150_000_000_000);
        assert_eq!(pb.px_policy_id, "ARRIVAL_MID");

        let transfer = CrossRecord {
            netting_cycle_id: d1_core::NettingCycleId::Direct,
            ..record
        };
        let pb = cross_record_to_pb(&transfer, run, "msg-2".to_string(), 43);
        assert_eq!(pb.netting_cycle_id, "DIRECT");
    }

    #[test]
    fn tracker_subject_matches_taxonomy() {
        assert_eq!(tracker_subject(BookId(1)), "d1.tracker.1");
    }

    #[test]
    fn tracker_record_to_pb_maps_all_fields() {
        let record = TrackerRecord {
            book: BookId(1),
            tracking_error_ann_e9: 67_349_822,
            tracking_diff_e9: 18_000_000,
            cash_weight_e9: 100_000_000,
            cash_drag_e9: 1_168_254,
            n_obs: 2,
            window_start_ns: 1_000_000_000,
            window_end_ns: 2_000_000_000,
        };
        let pb = tracker_record_to_pb(
            &record,
            Some(InstrumentId(2001)),
            60,
            "msg-1".to_string(),
            42,
        );

        assert_eq!(pb.meta.as_ref().unwrap().msg_id, "msg-1");
        assert_eq!(pb.meta.as_ref().unwrap().producer, "delta-one");
        assert_eq!(pb.meta.as_ref().unwrap().sent_ns, 42);
        assert_eq!(pb.book_id, 1);
        assert_eq!(pb.benchmark.as_ref().unwrap().instrument_id, 2001);
        assert_eq!(pb.kind, tracker_analytics::Kind::ExPost as i32);
        assert_eq!(pb.tracking_error_ann_e9, 67_349_822);
        assert_eq!(pb.tracking_diff_e9, 18_000_000);
        assert_eq!(pb.cash_weight_e9, 100_000_000);
        assert_eq!(pb.cash_drag_e9, 1_168_254);
        assert_eq!(pb.n_obs, 2);
        assert_eq!(pb.window_start_ns, 1_000_000_000);
        assert_eq!(pb.window_end_ns, 2_000_000_000);
        assert_eq!(pb.sampling_interval_s, 60);
    }

    #[test]
    fn tracker_record_to_pb_none_benchmark_leaves_field_unset() {
        let record = TrackerRecord {
            book: BookId(1),
            tracking_error_ann_e9: 0,
            tracking_diff_e9: 0,
            cash_weight_e9: 0,
            cash_drag_e9: 0,
            n_obs: 2,
            window_start_ns: 0,
            window_end_ns: 0,
        };
        let pb = tracker_record_to_pb(&record, None, 60, "msg-1".to_string(), 42);
        assert!(pb.benchmark.is_none());
    }

    #[test]
    fn exec_subject_matches_taxonomy() {
        assert_eq!(
            exec_subject(BookId(1), InstrumentId(1001)),
            "d1.exec.1.1001"
        );
    }

    #[test]
    fn exec_report_to_pb_maps_all_fields() {
        let report = ExecReport {
            cl_ord_id: ClOrdId::from_seq(42),
            exec_id: ExecId::from_short(b"EXEC-1").unwrap(),
            book: BookId(1),
            instrument: InstrumentId(1001),
            side: Side::Buy,
            status: OrderStatus::Filled,
            last_qty_e2: 10_000,
            last_px_e9: 150_500_000_000,
            cum_qty_e2: 10_000,
            leaves_qty_e2: 0,
            allocation: true,
            book_cum_qty_e2: 7_000,
            cycle_id: Some(5),
        };
        let run = RunId(uuid::Uuid::from_u128(0xabc));
        let pb = exec_report_to_pb(&report, run, "msg-1".to_string(), 42);
        assert_eq!(pb.meta.as_ref().unwrap().schema_version, 2);
        assert!(pb.allocation);
        assert_eq!(pb.book_cum_qty_e2, 7_000);
        assert_eq!(
            pb.netting_cycle_id,
            "00000000-0000-0000-0000-000000000abc/5"
        );

        assert_eq!(pb.meta.as_ref().unwrap().msg_id, "msg-1");
        assert_eq!(pb.meta.as_ref().unwrap().producer, "delta-one");
        assert_eq!(pb.meta.as_ref().unwrap().sent_ns, 42);
        assert_eq!(pb.cl_ord_id, "00000000000000000042");
        assert_eq!(pb.exec_id, "EXEC-1");
        assert_eq!(pb.book_id, 1);
        assert_eq!(pb.instrument.as_ref().unwrap().instrument_id, 1001);
        assert_eq!(pb.side, PbSide::Buy as i32);
        assert_eq!(pb.status, OrdStatus::Filled as i32);
        assert_eq!(pb.last_qty_e2, 10_000);
        assert_eq!(pb.last_px_e9, 150_500_000_000);
        assert_eq!(pb.cum_qty_e2, 10_000);
        assert_eq!(pb.leaves_qty_e2, 0);

        // A direct order (no minting cycle) carries "" and the zero defaults.
        let direct = ExecReport {
            allocation: false,
            book_cum_qty_e2: 0,
            cycle_id: None,
            ..report
        };
        let pb = exec_report_to_pb(&direct, run, "msg-2".to_string(), 43);
        assert!(!pb.allocation);
        assert_eq!(pb.book_cum_qty_e2, 0);
        assert_eq!(pb.netting_cycle_id, "");
    }

    #[test]
    fn marks_and_positions_subjects_match_taxonomy() {
        assert_eq!(marks_subject(InstrumentId(1001)), "d1.marks.1001");
        assert_eq!(positions_subject(BookId(3)), "d1.positions.3");
    }

    #[test]
    fn mark_to_pb_maps_all_fields() {
        let pb = mark_to_pb(
            &MarkUpdate {
                instrument: InstrumentId(1001),
                mid_px_e9: 150_000_000_000,
                feed_ts_ns: 12_000_000_000,
            },
            "m".to_string(),
            42,
        );
        assert_eq!(pb.meta.as_ref().unwrap().sent_ns, 42);
        assert_eq!(pb.instrument.as_ref().unwrap().instrument_id, 1001);
        assert_eq!(pb.mid_px_e9, 150_000_000_000);
        assert_eq!(pb.feed_ts_ns, 12_000_000_000);
    }

    #[test]
    fn positions_to_pb_maps_lines_and_empty_book() {
        let line = PositionUpdate {
            book: BookId(1),
            instrument: InstrumentId(1001),
            qty_e2: 100,
            avg_px_e9: 150,
            inflight_qty_e2: -5,
        };
        let pb = positions_to_pb(BookId(1), &[line], "m".to_string(), 7);
        assert_eq!(pb.book_id, 1);
        let l = pb.lines.first().unwrap();
        assert_eq!(l.instrument.as_ref().unwrap().instrument_id, 1001);
        assert_eq!((l.qty_e2, l.avg_px_e9, l.inflight_qty_e2), (100, 150, -5));
        assert!(
            positions_to_pb(BookId(2), &[], "m".to_string(), 7)
                .lines
                .is_empty()
        );
    }

    fn event(greek: Greek, level: Level) -> AlertEvent {
        AlertEvent {
            underlying: InstrumentId(1001),
            greek,
            level,
            value: 62,
            limit: 50,
            text: "t".to_string(),
        }
    }

    #[test]
    fn risk_limit_alert_gamma_uses_qty_e2_fields_and_vega_uses_e9() {
        let gamma = risk_limit_alert_to_pb(&event(Greek::Gamma, Level::Warn), "m".into(), 1);
        assert_eq!(gamma.greek, risk_limit_alert::Greek::Gamma as i32);
        assert_eq!((gamma.value_e9, gamma.limit_e9), (0, 0));
        assert_eq!((gamma.value_qty_e2, gamma.limit_qty_e2), (62, 50));
        let vega = risk_limit_alert_to_pb(&event(Greek::Vega, Level::Hard), "m".into(), 1);
        assert_eq!((vega.value_e9, vega.limit_e9), (62, 50));
        assert_eq!((vega.value_qty_e2, vega.limit_qty_e2), (0, 0));
        assert!(vega.breached && !vega.cleared);
    }

    #[test]
    fn schema_version_is_per_message_type() {
        let alert = risk_limit_alert_to_pb(&event(Greek::Vega, Level::Warn), "m".into(), 1);
        assert_eq!(alert.meta.unwrap().schema_version, 2);
        let mark = mark_to_pb(
            &MarkUpdate {
                instrument: InstrumentId(1001),
                mid_px_e9: 1,
                feed_ts_ns: 0,
            },
            "m".into(),
            1,
        );
        assert_eq!(mark.meta.unwrap().schema_version, 1);
        let snap = positions_to_pb(BookId(1), &[], "m".into(), 1);
        assert_eq!(snap.meta.unwrap().schema_version, 1);
    }

    #[test]
    fn risk_limit_alert_flags_follow_level() {
        let warn = risk_limit_alert_to_pb(&event(Greek::Rho, Level::Warn), "m".into(), 1);
        assert!(!warn.breached && !warn.cleared);
        let ok = risk_limit_alert_to_pb(&event(Greek::Rho, Level::Ok), "m".into(), 1);
        assert!(!ok.breached && ok.cleared);
    }

    #[test]
    fn valuation_subject_parses_numeric_book_only() {
        assert_eq!(
            valuation_book_from_subject("exo.valuations.3"),
            Some(BookId(3))
        );
        assert_eq!(valuation_book_from_subject("exo.valuations.EXO-SP"), None);
        assert_eq!(valuation_book_from_subject("exo.valuations."), None);
        assert_eq!(valuation_book_from_subject("exo.targets.3"), None);
        // Parseable but non-canonical tokens are rejected (ADR-014 §1).
        assert_eq!(valuation_book_from_subject("exo.valuations.+3"), None);
        assert_eq!(valuation_book_from_subject("exo.valuations.03"), None);
        assert_eq!(
            valuation_book_from_subject("exo.valuations.0"),
            Some(BookId(0))
        );
    }

    #[test]
    fn valuation_from_pb_reads_limit_fields_and_meta_completeness() {
        use crate::pb::hedging::common::v1::ValuationMeta;
        use crate::pb::hedging::live::v1::valuation_snapshot::ProductLine;
        let mut msg = ValuationSnapshot {
            book_id: 3,
            as_of_ns: 9,
            valuation: Some(ValuationMeta {
                model_id: "m".into(),
                params_hash: "p".into(),
                git_sha: "g".into(),
                ..Default::default()
            }),
            lines: vec![
                ProductLine {
                    underlying: Some(InstrumentRef {
                        instrument_id: 1001,
                        ..Default::default()
                    }),
                    vega_e9: 1,
                    gamma_qty_e2: 2,
                    rho_e9: 3,
                    delta_qty_e2: 4,
                    theta_e9: -5,
                    div_sens_e9: 6,
                    flagged_greeks: vec!["delta".into(), "gamma".into()],
                    ..Default::default()
                },
                ProductLine::default(),
            ],
            ..Default::default()
        };
        let v = valuation_from_pb(&msg);
        assert!(v.meta_complete);
        assert_eq!((v.book, v.as_of_ns), (BookId(3), 9));
        let l = v.lines.first().unwrap();
        assert_eq!(l.underlying, Some(InstrumentId(1001)));
        assert_eq!(
            (l.vega_e9, l.gamma_qty_e2, l.rho_e9, l.n_flagged),
            (1, 2, 3, 2)
        );
        assert_eq!((l.delta_qty_e2, l.theta_e9, l.div_sens_e9), (4, -5, 6));
        assert_eq!(v.lines.get(1).unwrap().underlying, None);
        msg.valuation.as_mut().unwrap().git_sha.clear();
        assert!(!valuation_from_pb(&msg).meta_complete);
        msg.valuation = None;
        assert!(!valuation_from_pb(&msg).meta_complete);
    }

    // ---- RiskSnapshot (ADR-014 Amendment 1) ----

    const AAPL: InstrumentId = InstrumentId(1001);
    const MSFT: InstrumentId = InstrumentId(1002);
    const IBM: InstrumentId = InstrumentId(1003);

    fn caps() -> TierOneLimits {
        TierOneLimits {
            vega_warn_e9: 50,
            vega_hard_e9: 100,
            gamma_warn_qty_e2: 20,
            gamma_hard_qty_e2: 50,
            rho_warn_e9: 10,
            rho_hard_e9: 25,
        }
    }

    fn fg(u: InstrumentId) -> FirmGreeks {
        FirmGreeks {
            underlying: u,
            exo_delta_qty_e2: 0,
            vega_e9: 0,
            gamma_qty_e2: 0,
            rho_e9: 0,
            theta_e9: 0,
            div_sens_e9: 0,
            flagged: false,
            vega: Level::Ok,
            gamma: Level::Ok,
            rho: Level::Ok,
            suspect: false,
        }
    }

    fn snap_of(
        exo: &[FirmGreeks],
        d1: &[(InstrumentId, i128)],
        mid: Option<i64>,
    ) -> crate::pb::hedging::live::v1::RiskSnapshot {
        let exo = exo.iter().map(|g| (g.underlying, *g)).collect();
        let d1 = d1.iter().copied().collect();
        risk_snapshot_to_pb(&exo, &d1, |_| mid, &caps(), "id".into(), 7)
    }

    fn row(
        s: &crate::pb::hedging::live::v1::RiskSnapshot,
        u: InstrumentId,
    ) -> &risk_snapshot::NetGreeks {
        s.greeks
            .iter()
            .find(|g| g.underlying.as_ref().map(|r| r.instrument_id) == Some(u.0))
            .unwrap()
    }

    #[test]
    fn empty_risk_snapshot_encodes_and_decodes() {
        use prost::Message as _;
        let s = snap_of(&[], &[], None);
        assert!(s.greeks.is_empty());
        let meta = s.meta.as_ref().unwrap();
        assert_eq!(
            (meta.schema_version, meta.sent_ns, meta.producer.as_str()),
            (2, 7, "delta-one")
        );
        let back = crate::pb::hedging::live::v1::RiskSnapshot::decode(&*s.encode_to_vec()).unwrap();
        assert_eq!(back, s);
    }

    #[test]
    fn risk_rows_are_the_union_and_flat_d1_alone_is_omitted() {
        let s = snap_of(
            &[fg(AAPL)],
            &[(MSFT, 5), (IBM, 0), (AAPL, 0)], // IBM flat: no row; AAPL has an EXO line
            None,
        );
        let ids: Vec<u32> = s
            .greeks
            .iter()
            .map(|g| g.underlying.as_ref().unwrap().instrument_id)
            .collect();
        assert_eq!(ids, vec![1001, 1002]);
    }

    #[test]
    fn firm_delta_is_d1_qty_plus_exo_delta_and_notional_uses_the_mark() {
        let mut g = fg(AAPL);
        g.exo_delta_qty_e2 = -300;
        // 1,000 (D1, e.g. across books) + (-300) EXO = 700 (x1e2 units).
        let s = snap_of(&[g], &[(AAPL, 1_000)], Some(150_000_000_000));
        let r = row(&s, AAPL);
        assert_eq!(r.firm_delta_qty_e2, 700);
        // 700e-2 units x 150.0 = 1050.0 -> 1_050_000_000_000 (e9).
        assert_eq!(r.notional_e9, 700 * 150_000_000_000 / 100);
        assert_eq!(r.notional_e9, 1_050_000_000_000);
        // No mark: delta still reported, notional 0.
        let s = snap_of(&[g], &[(AAPL, 1_000)], None);
        assert_eq!(
            (row(&s, AAPL).firm_delta_qty_e2, row(&s, AAPL).notional_e9),
            (700, 0)
        );
        // Short side: signs carry through.
        let s = snap_of(&[], &[(MSFT, -250)], Some(10_000_000_000));
        assert_eq!(row(&s, MSFT).firm_delta_qty_e2, -250);
        assert_eq!(row(&s, MSFT).notional_e9, -25_000_000_000);
    }

    #[test]
    fn risk_values_saturate_by_sign() {
        let big = i128::from(i64::MAX) * 4;
        let mut g = fg(AAPL);
        g.vega_e9 = big;
        g.gamma_qty_e2 = -big;
        g.theta_e9 = big;
        let s = snap_of(&[g], &[(AAPL, big)], Some(i64::MAX));
        let r = row(&s, AAPL);
        assert_eq!(r.vega_e9, i64::MAX);
        assert_eq!(r.gamma_qty_e2, i64::MIN);
        assert_eq!(r.theta_e9, i64::MAX);
        assert_eq!(r.firm_delta_qty_e2, i64::MAX);
        assert_eq!(r.notional_e9, i64::MAX);
        let s = snap_of(&[], &[(MSFT, -big)], Some(i64::MAX));
        assert_eq!(row(&s, MSFT).firm_delta_qty_e2, i64::MIN);
        assert_eq!(row(&s, MSFT).notional_e9, i64::MIN);
        // i128 overflow in the delta sum itself saturates, never wraps.
        let mut g = fg(AAPL);
        g.exo_delta_qty_e2 = i128::MAX;
        let s = snap_of(&[g], &[(AAPL, 10)], Some(i64::MAX));
        assert_eq!(row(&s, AAPL).firm_delta_qty_e2, i64::MAX);
        assert_eq!(row(&s, AAPL).notional_e9, i64::MAX);
    }

    #[test]
    fn risk_levels_flags_and_thresholds_come_from_inputs() {
        let mut g = fg(AAPL);
        g.vega = Level::Warn;
        g.gamma = Level::Hard;
        g.suspect = true;
        g.flagged = true;
        g.theta_e9 = -9;
        g.div_sens_e9 = 8;
        g.rho_e9 = -7;
        let exo = [(AAPL, g)].into_iter().collect();
        let other = TierOneLimits {
            vega_warn_e9: 1,
            vega_hard_e9: 2,
            gamma_warn_qty_e2: 3,
            gamma_hard_qty_e2: 4,
            rho_warn_e9: 5,
            rho_hard_e9: 6,
        };
        let s = risk_snapshot_to_pb(&exo, &BTreeMap::new(), |_| None, &other, "x".into(), 1);
        let r = row(&s, AAPL);
        use risk_snapshot::LimitLevel as L;
        assert_eq!(
            (r.vega_level, r.gamma_level, r.rho_level),
            (L::Warn as i32, L::Hard as i32, L::Ok as i32)
        );
        assert_eq!((r.vega_warn_e9, r.vega_hard_e9), (1, 2));
        assert_eq!((r.gamma_warn_qty_e2, r.gamma_hard_qty_e2), (3, 4));
        assert_eq!((r.rho_warn_e9, r.rho_hard_e9), (5, 6));
        assert!(r.limit_data_suspect && r.greeks_flagged);
        assert_eq!((r.theta_e9, r.div_sens_e9, r.rho_e9), (-9, 8, -7));
        // A D1-only row has OK levels, no flags, but still carries the caps.
        let s = snap_of(&[], &[(MSFT, 1)], None);
        let r = row(&s, MSFT);
        assert_eq!(
            (r.vega_level, r.gamma_level, r.rho_level),
            (L::Ok as i32, L::Ok as i32, L::Ok as i32)
        );
        assert!(!r.limit_data_suspect && !r.greeks_flagged);
        assert_eq!((r.vega_warn_e9, r.rho_hard_e9), (50, 25));
    }

    #[test]
    fn snapshot_from_a_limit_book_matches_hand_sums_and_the_alert_value() {
        use crate::limits::LimitBook;
        use std::time::{Duration, Instant};
        let mut lb = LimitBook::new(
            caps(),
            Duration::from_secs(15),
            &[BookId(3), BookId(4)],
            &[AAPL, MSFT],
        );
        let t = Instant::now();
        let l = |v, g, r, d, th, dv| ValLine {
            underlying: Some(AAPL),
            vega_e9: v,
            gamma_qty_e2: g,
            rho_e9: r,
            delta_qty_e2: d,
            theta_e9: th,
            div_sens_e9: dv,
            n_flagged: 0,
        };
        let mk = |book, lines| Valuation {
            book: BookId(book),
            as_of_ns: 1,
            meta_complete: true,
            lines,
        };
        let mut alerts =
            lb.on_snapshot(Some(BookId(3)), &mk(3, vec![l(30, 7, 3, 1_000, -5, 11)]), t);
        alerts.extend(lb.on_snapshot(Some(BookId(4)), &mk(4, vec![l(40, 8, 4, -300, -6, 12)]), t));
        let s = risk_snapshot_to_pb(
            &lb.firm_greeks(t),
            &BTreeMap::from([(AAPL, 50_i128)]),
            |_| Some(100_000_000_000),
            &lb.limits(),
            "m".into(),
            1,
        );
        let r = row(&s, AAPL);
        assert_eq!(r.vega_e9, 70);
        assert_eq!(r.gamma_qty_e2, 15);
        assert_eq!(r.rho_e9, 7);
        assert_eq!(r.theta_e9, -11);
        assert_eq!(r.div_sens_e9, 23);
        assert_eq!(r.firm_delta_qty_e2, 50 + 1_000 - 300);
        // The vega alert (70 >= warn 50) reports the same number the row does.
        let vega_alert = alerts.iter().rfind(|a| a.greek == Greek::Vega).unwrap();
        assert_eq!(vega_alert.value, r.vega_e9);
        assert_eq!(vega_alert.level, Level::Warn);
        assert_eq!(r.vega_level, risk_snapshot::LimitLevel::Warn as i32);
        assert_eq!(r.vega_warn_e9, 50);
    }

    #[test]
    fn health_status_to_pb_maps_all_fields() {
        let detail = BTreeMap::from([("nats".to_string(), "connected".to_string())]);
        let pb = health_status_to_pb(true, detail, "m".into(), 9);
        assert_eq!(pb.component, "delta-one");
        assert!(pb.ok);
        assert_eq!(pb.detail.len(), 1);
        assert_eq!(pb.detail.get("nats").map(String::as_str), Some("connected"));
        let meta = pb.meta.unwrap();
        assert_eq!((meta.schema_version, meta.sent_ns), (1, 9));
        assert_eq!(meta.producer, "delta-one");
        assert!(!health_status_to_pb(false, BTreeMap::new(), "m".into(), 1).ok);
    }
}
