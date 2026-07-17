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

/// Single-book demand: `target - current_qty_e2`, emitted as one market
/// order, or `None` when the target is already met. This is ADR-005's
/// `demand_b = target_b - position_b - inflight_b` with the `inflight_b`
/// term still missing.
///
/// ponytail: netting stand-in for P1.M3's `d1-netting` -- no in-flight
/// offset (so targets restated faster than fills return over-order), and no
/// cross-book netting or internal crosses at all. Both need firm-wide state
/// this seam doesn't have. Replaced by a call into `d1-netting` in P1.M3.
///
/// Because the target is absolute (root CLAUDE.md invariant #5), subtracting
/// the current position is what makes a restated target converge rather than
/// ratchet: an unchanged target yields no order once its fill is booked.
pub fn target_to_order(
    target: &Target,
    current_qty_e2: i64,
    next_cl_ord_id: ClOrdId,
) -> Option<Order> {
    // checked_sub: target and position are independently-sourced i64s, so
    // their difference can overflow even though neither operand does.
    let demand_e2 = target.target_qty_e2.checked_sub(current_qty_e2)?;

    let side = match demand_e2.signum() {
        1 => Side::Buy,
        -1 => Side::Sell,
        _ => return None, // target already met, nothing to do
    };

    // i64::MIN's magnitude doesn't fit back into i64 (unsigned_abs()
    // sidesteps the overflow a plain `.abs()` would panic on there) --
    // treat as unrepresentable rather than place a corrupted-quantity order.
    let order_qty_e2 = i64::try_from(demand_e2.unsigned_abs()).ok()?;

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
    use proptest::prelude::*;

    fn sample_target(target_qty_e2: i64) -> Target {
        Target {
            book: BookId(1),
            instrument: InstrumentId(1001),
            target_qty_e2,
        }
    }

    #[test]
    fn positive_target_from_flat_places_buy_order_for_the_full_quantity() {
        let target = sample_target(10_000);
        let order = target_to_order(&target, 0, ClOrdId::from_seq(1)).unwrap();
        assert_eq!(order.side, Side::Buy);
        assert_eq!(order.order_qty_e2, 10_000);
        assert_eq!(order.book, target.book);
        assert_eq!(order.instrument, target.instrument);
        assert_eq!(order.cl_ord_id, ClOrdId::from_seq(1));
    }

    #[test]
    fn negative_target_from_flat_places_sell_order_for_the_magnitude() {
        let target = sample_target(-5_000);
        let order = target_to_order(&target, 0, ClOrdId::from_seq(2)).unwrap();
        assert_eq!(order.side, Side::Sell);
        assert_eq!(order.order_qty_e2, 5_000);
    }

    #[test]
    fn flat_target_from_flat_places_no_order() {
        let target = sample_target(0);
        assert_eq!(target_to_order(&target, 0, ClOrdId::from_seq(3)), None);
    }

    #[test]
    fn order_status_starts_new_regardless_of_ordstore_place() {
        // `OrderStore::place` re-forces status/cum/leaves anyway (order.rs),
        // but a fresh `Order` should still be internally consistent before
        // it ever reaches the store.
        let target = sample_target(1_000);
        let order = target_to_order(&target, 0, ClOrdId::from_seq(4)).unwrap();
        assert_eq!(order.status, OrderStatus::New);
        assert_eq!(order.cum_qty_e2, 0);
    }

    #[test]
    fn target_already_met_places_no_order() {
        // The regression that matters: EXO restating an unchanged absolute
        // target must not ratchet the position (root CLAUDE.md #5).
        let target = sample_target(5_000);
        assert_eq!(target_to_order(&target, 5_000, ClOrdId::from_seq(5)), None);
    }

    #[test]
    fn target_above_position_buys_only_the_shortfall() {
        let target = sample_target(5_000);
        let order = target_to_order(&target, 3_000, ClOrdId::from_seq(6)).unwrap();
        assert_eq!(order.side, Side::Buy);
        assert_eq!(order.order_qty_e2, 2_000);
    }

    #[test]
    fn target_below_position_sells_the_excess() {
        let target = sample_target(5_000);
        let order = target_to_order(&target, 8_000, ClOrdId::from_seq(7)).unwrap();
        assert_eq!(order.side, Side::Sell);
        assert_eq!(order.order_qty_e2, 3_000);
    }

    #[test]
    fn target_flips_position_through_zero() {
        // Long 5_000, target short 5_000 -> one sell of the full 10_000 span.
        let target = sample_target(-5_000);
        let order = target_to_order(&target, 5_000, ClOrdId::from_seq(8)).unwrap();
        assert_eq!(order.side, Side::Sell);
        assert_eq!(order.order_qty_e2, 10_000);
    }

    #[test]
    fn unrepresentable_demand_places_no_order() {
        // target - position overflows i64 -> no order, rather than a
        // corrupted-quantity one.
        let target = sample_target(i64::MAX);
        assert_eq!(target_to_order(&target, -1, ClOrdId::from_seq(9)), None);
    }

    proptest! {
        /// Applying the emitted order's quantity to the current position
        /// always lands exactly on the target: the convergence property the
        /// ratcheting bug broke.
        #[test]
        fn emitted_order_closes_the_gap_exactly(
            target_qty in -1_000_000i64..=1_000_000,
            current_qty in -1_000_000i64..=1_000_000,
        ) {
            let target = sample_target(target_qty);
            let filled = match target_to_order(&target, current_qty, ClOrdId::from_seq(1)) {
                Some(order) => match order.side {
                    Side::Buy => current_qty + order.order_qty_e2,
                    Side::Sell => current_qty - order.order_qty_e2,
                },
                None => current_qty,
            };
            prop_assert_eq!(filled, target_qty);
        }
    }
}
