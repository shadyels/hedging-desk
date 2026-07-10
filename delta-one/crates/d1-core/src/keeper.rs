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
        let slot = self.slot(book, instrument)?;
        let pos = self.positions.get_mut(slot)?;

        let signed_qty = match side {
            Side::Buy => qty_e2,
            Side::Sell => qty_e2.checked_neg()?,
        };
        let new_qty = pos.net_qty_e2.checked_add(signed_qty)?;

        pos.avg_px_e9 = if new_qty == 0 {
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
        pos.net_qty_e2 = new_qty;
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
}
