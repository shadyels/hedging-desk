//! Edge converters between `d1-core` types and the NATS Protobuf wire
//! contract (`protocol/proto/`). Pure functions only — no client/session
//! state (that lives in `crate::lib`). ADR-004: proto never enters
//! `d1-core`; mirrors `d1-gateway-fix::convert`'s role for the FIX side.

use d1_core::{
    BookId, ClOrdId, CrossRecord, ExecId, ExecReport, InstrumentId, OrderStatus, Side, Target,
    TransferRequest,
};

use crate::error::NatsError;
use crate::pb::hedging::common::v1::{InstrumentRef, Meta, Side as PbSide};
use crate::pb::hedging::live::v1::{
    ExecutionReport, InternalCrossNotice, InternalTransferRequest, OrdStatus, TargetPosition,
};

/// Subject `InternalCrossNotice` publishes to (`protocol/nats-subjects.md`:
/// `d1.crosses`).
pub const CROSSES_SUBJECT: &str = "d1.crosses";

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

/// `d1-core` ids are fixed 20-byte ASCII arrays (zero-padded decimal, see
/// `ids.rs`); the proto fields are plain strings with no length cap, so a
/// lossy decode is exact for every id this system ever mints and never
/// needs the FIX side's reject-on-invalid-UTF8 handling.
fn clordid_to_string(id: &ClOrdId) -> String {
    String::from_utf8_lossy(&id.0).into_owned()
}

fn execid_to_string(id: &ExecId) -> String {
    String::from_utf8_lossy(&id.0).into_owned()
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
            exec_id: ExecId::from_bytes(*b"00000000000000EXEC-1"),
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
        assert_eq!(pb.exec_id, "00000000000000EXEC-1");
        assert_eq!(pb.book_id, 1);
        assert_eq!(pb.instrument.as_ref().unwrap().instrument_id, 1001);
        assert_eq!(pb.side, PbSide::Buy as i32);
        assert_eq!(pb.status, OrdStatus::Filled as i32);
        assert_eq!(pb.last_qty_e2, 10_000);
        assert_eq!(pb.last_px_e9, 150_500_000_000);
        assert_eq!(pb.cum_qty_e2, 10_000);
        assert_eq!(pb.leaves_qty_e2, 0);
    }
}
