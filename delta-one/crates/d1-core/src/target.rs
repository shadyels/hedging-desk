//! EXO target -> order, the netting-free M2 seam. Plain core type and pure
//! function only -- ADR-004: the proto `TargetPosition` never crosses into
//! `d1-core`; `d1-gateway-nats::convert` does that conversion at the edge,
//! exactly like `d1-gateway-fix::convert` does for FIX.

use crate::ids::{BookId, ClOrdId, InstrumentId};
use crate::keeper::Side;
use crate::order::{Order, OrderStatus};

/// A desired absolute position for one (book, instrument), as published by
/// EXO on `exo.targets.<book>.<instrument>` (`protocol/nats-subjects.md`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Target {
    /// Book this target applies to.
    pub book: BookId,
    /// Instrument being targeted.
    pub instrument: InstrumentId,
    /// Desired absolute position, fixed-point x10^2 -- **absolute**, not a
    /// delta-order (root CLAUDE.md invariant #5: EXO output is a target, D1
    /// decides what, if anything, to execute).
    pub target_qty_e2: i64,
}

/// ponytail: netting stand-in for P1.M3's `d1-netting` -- treats the target
/// as the entire desired position with no current-position/in-flight offset
/// (the real `net_external = target - position - inflight`,
/// delta-one/CLAUDE.md's netting spec, needs firm-wide state this seam
/// doesn't have). One market order for the target's full signed quantity, or
/// `None` when already flat. Replaced by a call into `d1-netting` in P1.M3.
pub fn target_to_order(target: &Target, next_cl_ord_id: ClOrdId) -> Option<Order> {
    let side = match target.target_qty_e2.signum() {
        1 => Side::Buy,
        -1 => Side::Sell,
        _ => return None, // flat: target_qty_e2 == 0, nothing to do
    };

    // i64::MIN's magnitude doesn't fit back into i64 (unsigned_abs()
    // sidesteps the overflow a plain `.abs()` would panic on there) --
    // treat as unrepresentable rather than place a corrupted-quantity order.
    let order_qty_e2 = i64::try_from(target.target_qty_e2.unsigned_abs()).ok()?;

    Some(Order {
        cl_ord_id: next_cl_ord_id,
        book: target.book,
        instrument: target.instrument,
        side,
        order_qty_e2,
        limit_px_e9: 0,
        status: OrderStatus::New,
        cum_qty_e2: 0,
        leaves_qty_e2: 0,
        last_px_e9: 0,
    })
}

#[cfg(test)]
#[allow(clippy::unwrap_used)] // tests: unwrap_used/expect_used are hot-path-only bans (delta-one/CLAUDE.md)
mod tests {
    use super::*;

    fn sample_target(target_qty_e2: i64) -> Target {
        Target {
            book: BookId(1),
            instrument: InstrumentId(1001),
            target_qty_e2,
        }
    }

    #[test]
    fn positive_target_places_buy_order_for_the_full_quantity() {
        let target = sample_target(10_000);
        let order = target_to_order(&target, ClOrdId::from_seq(1)).unwrap();
        assert_eq!(order.side, Side::Buy);
        assert_eq!(order.order_qty_e2, 10_000);
        assert_eq!(order.book, target.book);
        assert_eq!(order.instrument, target.instrument);
        assert_eq!(order.cl_ord_id, ClOrdId::from_seq(1));
    }

    #[test]
    fn negative_target_places_sell_order_for_the_magnitude() {
        let target = sample_target(-5_000);
        let order = target_to_order(&target, ClOrdId::from_seq(2)).unwrap();
        assert_eq!(order.side, Side::Sell);
        assert_eq!(order.order_qty_e2, 5_000);
    }

    #[test]
    fn flat_target_places_no_order() {
        let target = sample_target(0);
        assert_eq!(target_to_order(&target, ClOrdId::from_seq(3)), None);
    }

    #[test]
    fn order_status_starts_new_regardless_of_ordstore_place() {
        // `OrderStore::place` re-forces status/cum/leaves anyway (order.rs),
        // but a fresh `Order` should still be internally consistent before
        // it ever reaches the store.
        let target = sample_target(1_000);
        let order = target_to_order(&target, ClOrdId::from_seq(4)).unwrap();
        assert_eq!(order.status, OrderStatus::New);
        assert_eq!(order.cum_qty_e2, 0);
    }
}
