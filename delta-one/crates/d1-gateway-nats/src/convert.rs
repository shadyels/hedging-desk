//! Edge converters between `d1-core` types and the NATS Protobuf wire
//! contract (`protocol/proto/`). Pure functions only — no client/session
//! state (that lives in `crate::lib`). ADR-004: proto never enters
//! `d1-core`; mirrors `d1-gateway-fix::convert`'s role for the FIX side.

use d1_analytics::TrackerRecord;
use d1_core::{
    BookId, ClOrdId, CrossRecord, ExecId, ExecReport, InstrumentId, OrderStatus, Side, Target,
    TransferRequest,
};

use crate::error::NatsError;
use crate::limits::{AlertEvent, Greek, Level, ValLine, Valuation};
use crate::pb::hedging::common::v1::{InstrumentRef, Meta, Side as PbSide};
use crate::pb::hedging::live::v1::{
    ExecutionReport, InternalCrossNotice, InternalTransferRequest, MarkPrice, OrdStatus,
    PositionSnapshot, RiskLimitAlert, TargetPosition, TrackerAnalytics, ValuationSnapshot,
    position_snapshot, risk_limit_alert, tracker_analytics,
};
use crate::{MarkUpdate, PositionUpdate};

/// Subject `InternalCrossNotice` publishes to (`protocol/nats-subjects.md`:
/// `d1.crosses`).
pub const CROSSES_SUBJECT: &str = "d1.crosses";

/// Subject `RiskLimitAlert` publishes to (`protocol/nats-subjects.md`:
/// `d1.alerts.limits`).
pub const ALERTS_SUBJECT: &str = "d1.alerts.limits";

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
pub fn exec_report_to_pb(report: &ExecReport, msg_id: String, sent_ns: u64) -> ExecutionReport {
    ExecutionReport {
        meta: Some(Meta {
            msg_id,
            producer: "delta-one".to_string(),
            sent_ns,
            schema_version: 1,
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
    msg_id: String,
    sent_ns: u64,
) -> InternalCrossNotice {
    InternalCrossNotice {
        meta: Some(Meta {
            msg_id,
            producer: "delta-one".to_string(),
            sent_ns,
            schema_version: 1,
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
                n_flagged: l.flagged_greeks.len(),
            })
            .collect(),
    }
}

/// `Meta` with this message type's `schema_version` (per type, not global:
/// 2 for `RiskLimitAlert`, whose fields 7-10 were added in P2.M4, ADR-014 §4).
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
        };
        let pb = cross_record_to_pb(&record, "msg-1".to_string(), 42);

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
        };
        let pb = exec_report_to_pb(&report, "msg-1".to_string(), 42);

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
        assert_eq!(v.lines.get(1).unwrap().underlying, None);
        msg.valuation.as_mut().unwrap().git_sha.clear();
        assert!(!valuation_from_pb(&msg).meta_complete);
        msg.valuation = None;
        assert!(!valuation_from_pb(&msg).meta_complete);
    }
}
