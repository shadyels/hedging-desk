//! Position keeper: per (book, instrument) net quantity and average cost.
//!
//! Not exercised by the M1 replay demo — fills arrive with the M2 order path.
//! This module stands up the data structure and the `apply_fill` contract so
//! M2 has it ready (docs/ROADMAP.md P1.M1 decision log).

use std::collections::HashMap;

use crate::ids::{BookId, InstrumentId};

/// Buy/sell direction of a fill.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Side {
    /// Increases net position.
    Buy,
    /// Decreases net position.
    Sell,
}

/// Net position and average cost for one (book, instrument) pair.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Position {
    /// Net quantity, fixed-point ×10².
    pub net_qty_e2: i64,
    /// Average cost, fixed-point ×10⁹. Zero when flat.
    pub avg_px_e9: i64,
}

/// Flat `books x instruments` position table, preallocated at startup.
pub struct PositionKeeper {
    book_index: HashMap<BookId, usize>,
    instrument_index: HashMap<InstrumentId, usize>,
    n_instruments: usize,
    positions: Vec<Position>,
}

impl PositionKeeper {
    /// Preallocate a `books x instruments` position table. Startup allocation
    /// only; not called on the hot path.
    #[must_use]
    pub fn new(book_ids: &[BookId], instrument_ids: &[InstrumentId]) -> Self {
        let book_index = book_ids.iter().enumerate().map(|(i, b)| (*b, i)).collect();
        let instrument_index = instrument_ids
            .iter()
            .enumerate()
            .map(|(i, x)| (*x, i))
            .collect();
        Self {
            book_index,
            instrument_index,
            n_instruments: instrument_ids.len(),
            positions: vec![Position::default(); book_ids.len() * instrument_ids.len()],
        }
    }

    fn slot(&self, book: BookId, instrument: InstrumentId) -> Option<usize> {
        let &bi = self.book_index.get(&book)?;
        let &ii = self.instrument_index.get(&instrument)?;
        Some(bi * self.n_instruments + ii)
    }

    /// Pure fill computation: works out the slot index and the resulting
    /// `Position` for one (book, instrument) leg WITHOUT writing it back.
    /// `None` on overflow or an unknown book/instrument -- same failure
    /// modes as `apply_fill`, just deferred so a caller can compute two legs
    /// of a cross against current state before committing either
    /// (`apply_cross` below): committing one leg then discovering the other
    /// overflows would leave a half-booked, net-imbalanced firm position
    /// (root CLAUDE.md #2).
    fn compute_fill(
        &self,
        book: BookId,
        instrument: InstrumentId,
        side: Side,
        qty_e2: i64,
        px_e9: i64,
    ) -> Option<(usize, Position)> {
        let slot = self.slot(book, instrument)?;
        let pos = self.positions.get(slot)?;

        let signed_qty = match side {
            Side::Buy => qty_e2,
            Side::Sell => qty_e2.checked_neg()?,
        };
        let new_qty = pos.net_qty_e2.checked_add(signed_qty)?;

        let avg_px_e9 = if new_qty == 0 {
            0
        } else if pos.net_qty_e2 == 0
            || (pos.net_qty_e2.signum() == new_qty.signum()
                && new_qty.abs() >= pos.net_qty_e2.abs())
        {
            // Opening or adding to an existing position: weighted-average cost.
            let old_notional = pos.avg_px_e9.checked_mul(pos.net_qty_e2.abs())?;
            let add_notional = px_e9.checked_mul(qty_e2)?;
            old_notional
                .checked_add(add_notional)?
                .checked_div(new_qty.abs())?
        } else {
            // Reducing or flipping: cost basis of the remaining/new leg is this trade.
            px_e9
        };

        Some((
            slot,
            Position {
                net_qty_e2: new_qty,
                avg_px_e9,
            },
        ))
    }

    /// Apply a fill: `checked_add` on quantity, weighted-average cost on the
    /// held/adding side. Returns `None` on overflow or an unknown
    /// book/instrument; the keeper never panics.
    ///
    /// ponytail: cost-basis update is a simplified weighted-average (no lot
    /// tracking, no realized P&L). Full Σ-book-position invariants and
    /// per-book cash land with netting (P1.M3) / tracker analytics (P1.M5).
    pub fn apply_fill(
        &mut self,
        book: BookId,
        instrument: InstrumentId,
        side: Side,
        qty_e2: i64,
        px_e9: i64,
    ) -> Option<()> {
        let (slot, new_pos) = self.compute_fill(book, instrument, side, qty_e2, px_e9)?;
        if let Some(pos) = self.positions.get_mut(slot) {
            *pos = new_pos;
        }
        Some(())
    }

    /// Book a two-leg internal cross atomically: buy leg into `buy_book`,
    /// sell leg from `sell_book`, both computed against CURRENT state via
    /// `compute_fill` and committed only if BOTH succeed. Either the whole
    /// cross books or none of it does -- no rollback needed (and none
    /// attempted): `buy_book != sell_book` means the two legs land in
    /// disjoint slots (`slot = book * n_instruments + instrument`), so
    /// neither leg's computation reads state the other would write, and a
    /// reverse-`apply_fill` rollback would corrupt `avg_px_e9` anyway (the
    /// reducing branch overwrites it rather than restoring the prior value).
    /// `None` on overflow in either leg or an unknown book/instrument; the
    /// keeper never panics.
    #[must_use]
    pub fn apply_cross(
        &mut self,
        instrument: InstrumentId,
        buy_book: BookId,
        sell_book: BookId,
        qty_e2: i64,
        px_e9: i64,
    ) -> Option<()> {
        let (buy_slot, buy_pos) =
            self.compute_fill(buy_book, instrument, Side::Buy, qty_e2, px_e9)?;
        let (sell_slot, sell_pos) =
            self.compute_fill(sell_book, instrument, Side::Sell, qty_e2, px_e9)?;
        // Defensive only: the caller (transfer validation, ADR-005 netting)
        // already guarantees buy_book != sell_book, so these slots are
        // always disjoint. If that ever stops holding, committing into the
        // same slot twice would silently drop one leg's write -- reject
        // instead of risking that (the keeper never panics, so this is a
        // `None`, not a `debug_assert!`).
        if buy_slot == sell_slot {
            return None;
        }
        if let Some(pos) = self.positions.get_mut(buy_slot) {
            *pos = buy_pos;
        }
        if let Some(pos) = self.positions.get_mut(sell_slot) {
            *pos = sell_pos;
        }
        Some(())
    }

    /// Current position for a (book, instrument) pair, if both are configured.
    #[must_use]
    pub fn position(&self, book: BookId, instrument: InstrumentId) -> Option<Position> {
        let slot = self.slot(book, instrument)?;
        self.positions.get(slot).copied()
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)] // tests: unwrap_used/expect_used are hot-path-only bans (delta-one/CLAUDE.md)
mod tests {
    use super::*;
    use proptest::prelude::*;

    #[test]
    fn fill_then_inverse_returns_to_flat() {
        let mut keeper = PositionKeeper::new(&[BookId(1)], &[InstrumentId(1001)]);
        keeper
            .apply_fill(
                BookId(1),
                InstrumentId(1001),
                Side::Buy,
                10_000,
                150_000_000_000,
            ) // 100.00 units
            .unwrap();
        keeper
            .apply_fill(
                BookId(1),
                InstrumentId(1001),
                Side::Sell,
                10_000,
                150_000_000_000,
            ) // 100.00 units
            .unwrap();
        assert_eq!(
            keeper
                .position(BookId(1), InstrumentId(1001))
                .unwrap()
                .net_qty_e2,
            0
        );
    }

    #[test]
    fn unknown_book_or_instrument_returns_none() {
        let mut keeper = PositionKeeper::new(&[BookId(1)], &[InstrumentId(1001)]);
        assert_eq!(
            keeper.apply_fill(BookId(99), InstrumentId(1001), Side::Buy, 1, 1),
            None
        );
        assert_eq!(keeper.position(BookId(1), InstrumentId(9999)), None);
    }

    proptest! {
        #[test]
        fn fill_then_inverse_conserves_net_qty(qty in 1i64..=1_000_000, px in 1i64..=1_000_000_000_000) {
            let mut keeper = PositionKeeper::new(&[BookId(1)], &[InstrumentId(1001)]);
            keeper.apply_fill(BookId(1), InstrumentId(1001), Side::Buy, qty, px).unwrap();
            keeper.apply_fill(BookId(1), InstrumentId(1001), Side::Sell, qty, px).unwrap();
            prop_assert_eq!(
                keeper.position(BookId(1), InstrumentId(1001)).unwrap().net_qty_e2,
                0
            );
        }
    }

    #[test]
    fn apply_cross_happy_path_conserves_firm_position() {
        let mut keeper = PositionKeeper::new(&[BookId(1), BookId(2)], &[InstrumentId(1001)]);
        keeper
            .apply_cross(
                InstrumentId(1001),
                BookId(1),
                BookId(2),
                500,
                150_000_000_000,
            )
            .unwrap();
        let buy = keeper.position(BookId(1), InstrumentId(1001)).unwrap();
        let sell = keeper.position(BookId(2), InstrumentId(1001)).unwrap();
        assert_eq!(buy.net_qty_e2, 500);
        assert_eq!(sell.net_qty_e2, -500);
        assert_eq!(buy.net_qty_e2 + sell.net_qty_e2, 0, "both legs committed");
    }

    proptest! {
        #[test]
        fn apply_cross_is_atomic_on_leg_overflow(
            qty_e2 in 1i64..=1_000_000,
            px_e9 in 1i64..=1_000_000_000,
        ) {
            // Money-path integrity bug regression (security review): a cross
            // must never half-book. Force the buy leg to overflow by seeding
            // book 1 at i64::MAX -- any further Buy addition overflows
            // `checked_add` -- and assert `apply_cross` returns `None` AND
            // both books' positions are byte-for-byte unchanged, not just
            // that the buy leg was left alone while the sell leg silently
            // committed.
            let mut keeper = PositionKeeper::new(&[BookId(1), BookId(2)], &[InstrumentId(1001)]);
            keeper
                .apply_fill(BookId(1), InstrumentId(1001), Side::Buy, i64::MAX, 1)
                .unwrap();
            let before_buy = keeper.position(BookId(1), InstrumentId(1001)).unwrap();
            let before_sell = keeper.position(BookId(2), InstrumentId(1001)).unwrap();

            let result = keeper.apply_cross(InstrumentId(1001), BookId(1), BookId(2), qty_e2, px_e9);

            prop_assert_eq!(result, None);
            prop_assert_eq!(keeper.position(BookId(1), InstrumentId(1001)).unwrap(), before_buy);
            prop_assert_eq!(keeper.position(BookId(2), InstrumentId(1001)).unwrap(), before_sell);
        }
    }
}
