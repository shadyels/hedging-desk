//! Pure booking -> post-trade payload mapping (P1.M4 Slice 2). Isolates the
//! domain -> `d1_posttrade::PostTradeEvent` conversion from `run_core`'s
//! ring/thread wiring, so the mapping is unit-testable without a Kafka
//! producer or an `rtrb` ring. `crates/d1/src/lib.rs::run_core` calls these
//! at each booking site and pushes the results onto the `posttrade` ring.
//!
//! Demo-grade counterparty labels only (ADR-002 doesn't specify a real venue
//! registry yet).
// ponytail: hardcoded "INTERNAL"/"SIM" counterparty strings -- a real venue/
// broker registry is out of scope for M4; promote when a second real venue
// exists.
const COUNTERPARTY_INTERNAL: &str = "INTERNAL";
const COUNTERPARTY_SIM: &str = "SIM";

use d1_core::{BookId, ClOrdId, CrossRecord, ExecId, InstrumentId, OrderStatus, Side};
use d1_posttrade::{
    Allocation, AuditOrigin, Cross, NettingCycleId, OrderAudit, PostTradeEvent, TradeKind, TradeLeg,
};

/// One booked internal cross (ADR-005): the `Cross` record itself plus its
/// two `TradeLeg`s (buy leg, then sell leg), each `TradeKind::InternalCrossLeg`
/// and stamped with the same `cross_id`.
#[must_use]
pub fn cross_events(cross: &CrossRecord, cycle: NettingCycleId) -> [PostTradeEvent; 3] {
    let buy_leg = TradeLeg {
        book: cross.buy_book,
        instrument: cross.instrument,
        side: Side::Buy,
        qty_e2: cross.qty_e2,
        px_e9: cross.ref_px_e9,
        kind: TradeKind::InternalCrossLeg,
        cross_id: Some(cross.cross_id),
        parent_cl_ord_id: None,
        exec_id: None,
        counterparty: COUNTERPARTY_INTERNAL,
    };
    let sell_leg = TradeLeg {
        book: cross.sell_book,
        side: Side::Sell,
        ..buy_leg
    };

    [
        PostTradeEvent::Cross(Cross {
            cross_id: cross.cross_id,
            instrument: cross.instrument,
            buy_book: cross.buy_book,
            sell_book: cross.sell_book,
            qty_e2: cross.qty_e2,
            ref_px_e9: cross.ref_px_e9,
            policy_id: cross.policy_id,
            netting_cycle_id: cycle,
        }),
        PostTradeEvent::Trade(buy_leg),
        PostTradeEvent::Trade(sell_leg),
    ]
}

/// A pro-rata allocation of an external fill back to `book` (ADR-005),
/// carrying the netting cycle that produced the parent order.
#[must_use]
#[allow(clippy::too_many_arguments)]
pub fn allocation_event(
    parent_cl_ord_id: ClOrdId,
    exec_id: ExecId,
    instrument: InstrumentId,
    book: BookId,
    qty_e2: i64,
    px_e9: i64,
    cycle: NettingCycleId,
) -> PostTradeEvent {
    PostTradeEvent::Allocation(Allocation {
        parent_cl_ord_id,
        exec_id,
        instrument,
        book,
        qty_e2,
        px_e9,
        netting_cycle_id: cycle,
    })
}

/// A booked fill against an external venue (`TradeKind::ExternalFill`),
/// demo-labeled counterparty `"SIM"`.
#[must_use]
#[allow(clippy::too_many_arguments)]
pub fn external_fill_trade(
    book: BookId,
    instrument: InstrumentId,
    side: Side,
    qty_e2: i64,
    px_e9: i64,
    exec_id: ExecId,
    parent_cl_ord_id: ClOrdId,
) -> PostTradeEvent {
    PostTradeEvent::Trade(TradeLeg {
        book,
        instrument,
        side,
        qty_e2,
        px_e9,
        kind: TradeKind::ExternalFill,
        cross_id: None,
        parent_cl_ord_id: Some(parent_cl_ord_id),
        exec_id: Some(exec_id),
        counterparty: COUNTERPARTY_SIM,
    })
}

/// An order state transition, for compliance replay.
#[must_use]
#[allow(clippy::too_many_arguments)]
pub fn order_audit(
    cl_ord_id: ClOrdId,
    instrument: InstrumentId,
    side: Side,
    from: OrderStatus,
    to: OrderStatus,
    qty_e2: i64,
    cum_qty_e2: i64,
    leaves_qty_e2: i64,
    origin: AuditOrigin,
    detail: Option<&'static str>,
) -> PostTradeEvent {
    PostTradeEvent::OrderAudit(OrderAudit {
        cl_ord_id,
        instrument,
        side,
        from_status: from,
        to_status: to,
        qty_e2,
        cum_qty_e2,
        leaves_qty_e2,
        origin,
        detail,
    })
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)] // tests: hot-path-only bans (delta-one/CLAUDE.md)
mod tests {
    use super::*;

    fn sample_cross() -> CrossRecord {
        CrossRecord {
            cross_id: uuid::Uuid::now_v7(),
            instrument: InstrumentId(1001),
            buy_book: BookId(1),
            sell_book: BookId(2),
            qty_e2: 80_000,
            ref_px_e9: 150_000_000_000,
            policy_id: "ARRIVAL_MID",
        }
    }

    #[test]
    fn cross_events_has_cross_plus_two_legs_with_correct_sides_and_books() {
        let cross = sample_cross();
        let events = cross_events(&cross, NettingCycleId::Cycle(7));

        let PostTradeEvent::Cross(c) = &events[0] else {
            panic!("expected Cross first")
        };
        assert_eq!(c.cross_id, cross.cross_id);
        assert_eq!(c.instrument, cross.instrument);
        assert_eq!(c.buy_book, BookId(1));
        assert_eq!(c.sell_book, BookId(2));
        assert_eq!(c.qty_e2, 80_000);
        assert_eq!(c.ref_px_e9, 150_000_000_000);
        assert_eq!(c.policy_id, "ARRIVAL_MID");
        assert_eq!(c.netting_cycle_id, NettingCycleId::Cycle(7));

        let PostTradeEvent::Trade(buy_leg) = &events[1] else {
            panic!("expected buy Trade leg second")
        };
        assert_eq!(buy_leg.book, BookId(1));
        assert_eq!(buy_leg.side, Side::Buy);
        assert_eq!(buy_leg.kind, TradeKind::InternalCrossLeg);
        assert_eq!(buy_leg.cross_id, Some(cross.cross_id));
        assert_eq!(buy_leg.parent_cl_ord_id, None);
        assert_eq!(buy_leg.exec_id, None);
        assert_eq!(buy_leg.counterparty, "INTERNAL");
        assert_eq!(buy_leg.qty_e2, 80_000);
        assert_eq!(buy_leg.px_e9, 150_000_000_000);

        let PostTradeEvent::Trade(sell_leg) = &events[2] else {
            panic!("expected sell Trade leg third")
        };
        assert_eq!(sell_leg.book, BookId(2));
        assert_eq!(sell_leg.side, Side::Sell);
        assert_eq!(sell_leg.kind, TradeKind::InternalCrossLeg);
        assert_eq!(sell_leg.cross_id, Some(cross.cross_id));
        assert_eq!(sell_leg.counterparty, "INTERNAL");
    }

    #[test]
    fn cross_events_direct_transfer_stamps_direct_cycle() {
        let cross = sample_cross();
        let events = cross_events(&cross, NettingCycleId::Direct);
        let PostTradeEvent::Cross(c) = &events[0] else {
            panic!("expected Cross first")
        };
        assert_eq!(c.netting_cycle_id, NettingCycleId::Direct);
    }

    #[test]
    fn allocation_event_carries_parent_cycle_id_and_fields() {
        let event = allocation_event(
            ClOrdId::from_seq(1),
            ExecId::from_bytes([9; 20]),
            InstrumentId(1001),
            BookId(3),
            40_000,
            150_500_000_000,
            NettingCycleId::Cycle(9),
        );
        let PostTradeEvent::Allocation(a) = event else {
            panic!("expected Allocation")
        };
        assert_eq!(a.parent_cl_ord_id, ClOrdId::from_seq(1));
        assert_eq!(a.exec_id, ExecId::from_bytes([9; 20]));
        assert_eq!(a.instrument, InstrumentId(1001));
        assert_eq!(a.book, BookId(3));
        assert_eq!(a.qty_e2, 40_000);
        assert_eq!(a.px_e9, 150_500_000_000);
        assert_eq!(a.netting_cycle_id, NettingCycleId::Cycle(9));
    }

    #[test]
    fn external_fill_trade_is_external_kind_with_sim_counterparty() {
        let event = external_fill_trade(
            BookId(1),
            InstrumentId(1001),
            Side::Buy,
            10_000,
            150_000_000_000,
            ExecId::from_bytes([2; 20]),
            ClOrdId::from_seq(1),
        );
        let PostTradeEvent::Trade(leg) = event else {
            panic!("expected Trade")
        };
        assert_eq!(leg.book, BookId(1));
        assert_eq!(leg.instrument, InstrumentId(1001));
        assert_eq!(leg.side, Side::Buy);
        assert_eq!(leg.qty_e2, 10_000);
        assert_eq!(leg.px_e9, 150_000_000_000);
        assert_eq!(leg.kind, TradeKind::ExternalFill);
        assert_eq!(leg.cross_id, None);
        assert_eq!(leg.parent_cl_ord_id, Some(ClOrdId::from_seq(1)));
        assert_eq!(leg.exec_id, Some(ExecId::from_bytes([2; 20])));
        assert_eq!(leg.counterparty, "SIM");
    }

    #[test]
    fn order_audit_from_to_and_origin_set_correctly() {
        let event = order_audit(
            ClOrdId::from_seq(5),
            InstrumentId(1001),
            Side::Buy,
            OrderStatus::New,
            OrderStatus::PartiallyFilled,
            10_000,
            10_000,
            90_000,
            AuditOrigin::NettingEngine,
            None,
        );
        let PostTradeEvent::OrderAudit(a) = event else {
            panic!("expected OrderAudit")
        };
        assert_eq!(a.cl_ord_id, ClOrdId::from_seq(5));
        assert_eq!(a.instrument, InstrumentId(1001));
        assert_eq!(a.side, Side::Buy);
        assert_eq!(a.from_status, OrderStatus::New);
        assert_eq!(a.to_status, OrderStatus::PartiallyFilled);
        assert_eq!(a.qty_e2, 10_000);
        assert_eq!(a.cum_qty_e2, 10_000);
        assert_eq!(a.leaves_qty_e2, 90_000);
        assert_eq!(a.origin, AuditOrigin::NettingEngine);
        assert_eq!(a.detail, None);
    }

    #[test]
    fn order_audit_detail_carries_reject_reason() {
        let event = order_audit(
            ClOrdId::from_seq(6),
            InstrumentId(1001),
            Side::Sell,
            OrderStatus::New,
            OrderStatus::Rejected,
            0,
            0,
            0,
            AuditOrigin::System,
            Some("risk limit breach"),
        );
        let PostTradeEvent::OrderAudit(a) = event else {
            panic!("expected OrderAudit")
        };
        assert_eq!(a.to_status, OrderStatus::Rejected);
        assert_eq!(a.origin, AuditOrigin::System);
        assert_eq!(a.detail, Some("risk limit breach"));
    }
}
