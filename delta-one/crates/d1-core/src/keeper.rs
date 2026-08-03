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

/// Flat `books x instruments` position table plus per-book cash,
/// preallocated at startup.
pub struct PositionKeeper {
    book_index: HashMap<BookId, usize>,
    instrument_index: HashMap<InstrumentId, usize>,
    n_instruments: usize,
    positions: Vec<Position>,
    /// Per-BOOK cash balance, `_e9` fixed point, indexed by `book_index`
    /// (length = n_books, NOT n_books * n_instruments -- cash has no
    /// per-instrument dimension).
    cash_e9: Vec<i64>,
    /// Instrument ids in universe order, for `positions_for_book`'s
    /// zero-alloc row iteration (mirrors `MarketData`'s `ids` field).
    instrument_ids: Vec<InstrumentId>,
}

impl PositionKeeper {
    /// Preallocate a `books x instruments` position table plus per-book
    /// cash. Startup allocation only; not called on the hot path.
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
            cash_e9: vec![0; book_ids.len()],
            instrument_ids: instrument_ids.to_vec(),
        }
    }

    /// Resolve a (book, instrument) pair to its book index and its flat
    /// position-table slot (`book_idx * n_instruments + instrument_idx`).
    fn slot(&self, book: BookId, instrument: InstrumentId) -> Option<(usize, usize)> {
        let &bi = self.book_index.get(&book)?;
        let &ii = self.instrument_index.get(&instrument)?;
        Some((bi, bi * self.n_instruments + ii))
    }

    /// Pure fill computation: works out the position slot, the resulting
    /// `Position`, the book index, and the resulting absolute cash balance
    /// for one (book, instrument) leg WITHOUT writing any of it back. `None`
    /// on overflow (position or cash) or an unknown book/instrument -- same
    /// failure modes as `apply_fill`, just deferred so a caller can compute
    /// two legs of a cross against current state before committing either
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
    ) -> Option<(usize, Position, usize, i64)> {
        let (book_idx, slot) = self.slot(book, instrument)?;
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

        // Cash effect of this fill: Buy pays (cash decreases), Sell receives
        // (cash increases) -- `signed_qty` already carries that sign.
        // `qty_e2 * px_e9` reaches ~9e30, far past `i64`, so the product and
        // negation happen in `i128`, narrowed back to `i64` with a checked
        // cast that returns `None` on overflow exactly like the position
        // arithmetic above. Rust integer division truncates toward zero, so
        // `(-x) / 100 == -(x / 100)` exactly: this is load-bearing -- it
        // makes a buy then an inverse sell restore cash bit-for-bit, and it
        // makes a cross's two legs (opposite `signed_qty`, same `px_e9`) sum
        // to exactly zero firm cash.
        //
        // The `i64` fast path is a LATENCY fix, not a style choice: LLVM has
        // no `i128` divide instruction to lower `/ 100` to, so the `i128`
        // arm compiles to a `__divti3` compiler-rt libcall. Do NOT
        // "simplify" this back into a bare `i128` divide.
        let cash_delta_e9 = match signed_qty.checked_mul(px_e9) {
            // Bit-exact with the `i128` arm: when the product fits in `i64`
            // both arms divide the SAME value by 100 with the same
            // truncation-toward-zero, so the two paths cannot disagree.
            Some(product) => -(product / 100),
            None => i64::try_from(-(i128::from(signed_qty) * i128::from(px_e9)) / 100).ok()?,
        };
        let cash = self.cash_e9.get(book_idx)?;
        let new_cash = cash.checked_add(cash_delta_e9)?;

        Some((
            slot,
            Position {
                net_qty_e2: new_qty,
                avg_px_e9,
            },
            book_idx,
            new_cash,
        ))
    }

    /// Apply a fill: `checked_add` on quantity, weighted-average cost on the
    /// held/adding side, and the matching cash movement (buy pays, sell
    /// receives). Returns `None` on overflow (position or cash) or an
    /// unknown book/instrument; the keeper never panics.
    ///
    /// ponytail: cost-basis update is a simplified weighted-average (no lot
    /// tracking, no realized P&L). Per-book cash is now tracked (P1.M5
    /// Slice 1); full Σ-book-position invariants remain future work.
    pub fn apply_fill(
        &mut self,
        book: BookId,
        instrument: InstrumentId,
        side: Side,
        qty_e2: i64,
        px_e9: i64,
    ) -> Option<()> {
        let (slot, new_pos, book_idx, new_cash) =
            self.compute_fill(book, instrument, side, qty_e2, px_e9)?;
        if let Some(pos) = self.positions.get_mut(slot) {
            *pos = new_pos;
        }
        if let Some(cash) = self.cash_e9.get_mut(book_idx) {
            *cash = new_cash;
        }
        Some(())
    }

    /// Book a two-leg internal cross atomically: buy leg into `buy_book`,
    /// sell leg from `sell_book`, both computed against CURRENT state via
    /// `compute_fill` and committed only if BOTH succeed (position AND
    /// cash). Either the whole cross books or none of it does -- no
    /// rollback needed (and none attempted): `buy_book != sell_book` means
    /// the two legs land in disjoint POSITION slots (`slot = book_idx *
    /// n_instruments + instrument_idx`), and since `book_idx` is exactly the
    /// index into `cash_e9` too, `buy_slot == sell_slot` (checked below)
    /// happens iff `buy_book_idx == sell_book_idx` -- so the existing
    /// disjointness guard already proves the CASH slots are disjoint as
    /// well, and no second check is needed. A reverse-`apply_fill` rollback
    /// would corrupt `avg_px_e9` anyway (the reducing branch overwrites it
    /// rather than restoring the prior value). `None` on overflow in either
    /// leg (position or cash) or an unknown book/instrument; the keeper
    /// never panics.
    #[must_use]
    pub fn apply_cross(
        &mut self,
        instrument: InstrumentId,
        buy_book: BookId,
        sell_book: BookId,
        qty_e2: i64,
        px_e9: i64,
    ) -> Option<()> {
        let (buy_slot, buy_pos, buy_book_idx, buy_cash) =
            self.compute_fill(buy_book, instrument, Side::Buy, qty_e2, px_e9)?;
        let (sell_slot, sell_pos, sell_book_idx, sell_cash) =
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
        if let Some(cash) = self.cash_e9.get_mut(buy_book_idx) {
            *cash = buy_cash;
        }
        if let Some(cash) = self.cash_e9.get_mut(sell_book_idx) {
            *cash = sell_cash;
        }
        Some(())
    }

    /// Current position for a (book, instrument) pair, if both are configured.
    #[must_use]
    pub fn position(&self, book: BookId, instrument: InstrumentId) -> Option<Position> {
        let (_, slot) = self.slot(book, instrument)?;
        self.positions.get(slot).copied()
    }

    /// Seed a book's cash balance to an absolute value. Startup-only entry
    /// point (each tracker book's `initial_cash_e9` from refdata is seeded
    /// once before any fills are applied); not called mid-session.
    #[must_use]
    pub fn seed_cash(&mut self, book: BookId, cash_e9: i64) -> Option<()> {
        let &bi = self.book_index.get(&book)?;
        let cash = self.cash_e9.get_mut(bi)?;
        *cash = cash_e9;
        Some(())
    }

    /// Current cash balance for a book, if configured.
    #[must_use]
    pub fn cash(&self, book: BookId) -> Option<i64> {
        let &bi = self.book_index.get(&book)?;
        self.cash_e9.get(bi).copied()
    }

    /// Every (instrument, position) pair for one book, in universe order,
    /// including flat positions. Zero-alloc: slices the book's row out of
    /// the flat `positions` table (`.get(start..start + n_instruments)`,
    /// never `[]` -- `indexing_slicing` is denied) and zips it with the
    /// instrument-id list built at construction, mirroring
    /// `MarketData::iter`'s shape.
    pub fn positions_for_book(
        &self,
        book: BookId,
    ) -> Option<impl Iterator<Item = (InstrumentId, Position)> + '_> {
        let &bi = self.book_index.get(&book)?;
        let start = bi * self.n_instruments;
        let row = self.positions.get(start..start + self.n_instruments)?;
        Some(self.instrument_ids.iter().copied().zip(row.iter().copied()))
    }

    /// Credit a per-share dividend to every book holding `instrument`,
    /// proportional to net position: `cash += net_qty_e2 * div_per_share_e9
    /// / 100`. All-or-nothing WITHOUT allocating: pass 1 recomputes every
    /// book's checked arithmetic and discards the result, so a `None`
    /// (overflow on any single book) rejects the whole credit before
    /// anything is written; pass 2 repeats the identical arithmetic and
    /// applies it. No `Vec` of pending values is built -- the arithmetic is
    /// cheap enough to redo rather than to buffer.
    #[must_use]
    pub fn credit_dividend(
        &mut self,
        instrument: InstrumentId,
        div_per_share_e9: i64,
    ) -> Option<()> {
        let &ii = self.instrument_index.get(&instrument)?;
        let n_books = self.cash_e9.len();

        // Pass 1: validate every book; bail on the first overflow, nothing
        // written yet.
        for bi in 0..n_books {
            let slot = bi.checked_mul(self.n_instruments)?.checked_add(ii)?;
            let pos = self.positions.get(slot)?;
            let cash = self.cash_e9.get(bi)?;
            let delta =
                i64::try_from(i128::from(pos.net_qty_e2) * i128::from(div_per_share_e9) / 100)
                    .ok()?;
            cash.checked_add(delta)?;
        }

        // Pass 2: recompute (pass 1 already proved every step below
        // succeeds) and apply.
        for bi in 0..n_books {
            let slot = bi.checked_mul(self.n_instruments)?.checked_add(ii)?;
            let pos = self.positions.get(slot)?;
            let delta =
                i64::try_from(i128::from(pos.net_qty_e2) * i128::from(div_per_share_e9) / 100)
                    .ok()?;
            let cash = self.cash_e9.get_mut(bi)?;
            *cash = cash.checked_add(delta)?;
        }
        Some(())
    }

    /// Accrue one sampling period of cash yield: `cash += cash *
    /// yield_annual_e9 / (1e9 * periods_per_year)`. Computed in `i128`:
    /// cash ~1e16 times yield ~4e7 is ~4e23, past `i64::MAX` before the
    /// divide. `periods_per_year` is a caller parameter, not a constant
    /// here -- `d1-core` must not depend on `d1-analytics`, which is where
    /// the sampling frequency (252/year, Slice 2) is decided.
    #[must_use]
    pub fn accrue_cash(
        &mut self,
        book: BookId,
        yield_annual_e9: i64,
        periods_per_year: i64,
    ) -> Option<()> {
        let &bi = self.book_index.get(&book)?;
        let cash = *self.cash_e9.get(bi)?;
        let denom = i128::from(1_000_000_000i64).checked_mul(i128::from(periods_per_year))?;
        if denom == 0 {
            return None;
        }
        let accrual = i128::from(cash)
            .checked_mul(i128::from(yield_annual_e9))?
            .checked_div(denom)?;
        let new_cash = i64::try_from(i128::from(cash).checked_add(accrual)?).ok()?;
        let cash = self.cash_e9.get_mut(bi)?;
        *cash = new_cash;
        Some(())
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
            // both books' positions AND cash are byte-for-byte unchanged,
            // not just that the buy leg was left alone while the sell leg
            // silently committed.
            let mut keeper = PositionKeeper::new(&[BookId(1), BookId(2)], &[InstrumentId(1001)]);
            keeper
                .apply_fill(BookId(1), InstrumentId(1001), Side::Buy, i64::MAX, 1)
                .unwrap();
            let before_buy = keeper.position(BookId(1), InstrumentId(1001)).unwrap();
            let before_sell = keeper.position(BookId(2), InstrumentId(1001)).unwrap();
            let before_buy_cash = keeper.cash(BookId(1)).unwrap();
            let before_sell_cash = keeper.cash(BookId(2)).unwrap();

            let result = keeper.apply_cross(InstrumentId(1001), BookId(1), BookId(2), qty_e2, px_e9);

            prop_assert_eq!(result, None);
            prop_assert_eq!(keeper.position(BookId(1), InstrumentId(1001)).unwrap(), before_buy);
            prop_assert_eq!(keeper.position(BookId(2), InstrumentId(1001)).unwrap(), before_sell);
            prop_assert_eq!(keeper.cash(BookId(1)).unwrap(), before_buy_cash);
            prop_assert_eq!(keeper.cash(BookId(2)).unwrap(), before_sell_cash);
        }
    }

    #[test]
    fn apply_cross_atomic_on_cash_overflow_with_positions_fine() {
        // Positions have plenty of headroom, but buy_book's cash starts at
        // i64::MIN -- the buy leg's cash effect (buy pays, cash decreases)
        // underflows even though both legs' POSITION arithmetic succeeds.
        // Atomicity must hold for a cash-only overflow too, not just a
        // position overflow.
        let mut keeper = PositionKeeper::new(&[BookId(1), BookId(2)], &[InstrumentId(1001)]);
        keeper.seed_cash(BookId(1), i64::MIN).unwrap();
        let before_buy = keeper.position(BookId(1), InstrumentId(1001)).unwrap();
        let before_sell = keeper.position(BookId(2), InstrumentId(1001)).unwrap();
        let before_buy_cash = keeper.cash(BookId(1)).unwrap();
        let before_sell_cash = keeper.cash(BookId(2)).unwrap();

        let result = keeper.apply_cross(InstrumentId(1001), BookId(1), BookId(2), 100, 1);

        assert_eq!(result, None);
        assert_eq!(
            keeper.position(BookId(1), InstrumentId(1001)).unwrap(),
            before_buy
        );
        assert_eq!(
            keeper.position(BookId(2), InstrumentId(1001)).unwrap(),
            before_sell
        );
        assert_eq!(keeper.cash(BookId(1)).unwrap(), before_buy_cash);
        assert_eq!(keeper.cash(BookId(2)).unwrap(), before_sell_cash);
    }

    proptest! {
        #[test]
        fn fill_then_inverse_conserves_cash(qty in 1i64..=1_000_000, px in 1i64..=1_000_000_000_000) {
            let mut keeper = PositionKeeper::new(&[BookId(1)], &[InstrumentId(1001)]);
            let before = keeper.cash(BookId(1)).unwrap();
            keeper.apply_fill(BookId(1), InstrumentId(1001), Side::Buy, qty, px).unwrap();
            keeper.apply_fill(BookId(1), InstrumentId(1001), Side::Sell, qty, px).unwrap();
            prop_assert_eq!(keeper.cash(BookId(1)).unwrap(), before);
        }
    }

    proptest! {
        #[test]
        fn apply_cross_conserves_firm_cash(qty in 1i64..=1_000_000, px in 1i64..=1_000_000_000_000) {
            let mut keeper = PositionKeeper::new(&[BookId(1), BookId(2)], &[InstrumentId(1001)]);
            let before_total = keeper.cash(BookId(1)).unwrap() + keeper.cash(BookId(2)).unwrap();
            keeper
                .apply_cross(InstrumentId(1001), BookId(1), BookId(2), qty, px)
                .unwrap();
            let after_total = keeper.cash(BookId(1)).unwrap() + keeper.cash(BookId(2)).unwrap();
            prop_assert_eq!(after_total, before_total);
        }
    }

    proptest! {
        #[test]
        fn credit_dividend_is_all_or_nothing(
            qty_e2 in 1i64..=1_000_000,
            div_per_share_e9 in 1i64..=1_000_000_000,
        ) {
            let mut keeper = PositionKeeper::new(&[BookId(1), BookId(2)], &[InstrumentId(1001)]);
            // book1: an ordinary position that would receive a real credit
            // if the dividend were not rejected.
            keeper.apply_fill(BookId(1), InstrumentId(1001), Side::Buy, qty_e2, 0).unwrap();
            // book2: the same position, but cash pinned at i64::MAX so ANY
            // positive credit overflows -- forcing the whole dividend to
            // reject, not just book2's share of it.
            keeper.apply_fill(BookId(2), InstrumentId(1001), Side::Buy, qty_e2, 0).unwrap();
            keeper.seed_cash(BookId(2), i64::MAX).unwrap();
            let before_book1_cash = keeper.cash(BookId(1)).unwrap();
            let before_book2_cash = keeper.cash(BookId(2)).unwrap();

            let result = keeper.credit_dividend(InstrumentId(1001), div_per_share_e9);

            prop_assert_eq!(result, None);
            prop_assert_eq!(
                keeper.cash(BookId(1)).unwrap(),
                before_book1_cash,
                "all-or-nothing: book1 must not be credited when book2 overflows"
            );
            prop_assert_eq!(keeper.cash(BookId(2)).unwrap(), before_book2_cash);
        }
    }

    #[test]
    fn accrue_cash_matches_hand_computed_demo_yield() {
        let mut keeper = PositionKeeper::new(&[BookId(1)], &[InstrumentId(1001)]);
        keeper.seed_cash(BookId(1), 10_000_000_000_000_000).unwrap();
        keeper.accrue_cash(BookId(1), 40_000_000, 252).unwrap();
        // Hand computation: 10_000_000_000_000_000 * 40_000_000 /
        // (1_000_000_000 * 252) = 4e23 / 2.52e11 = 1_587_301_587_301.587...,
        // truncated toward zero (positive, so floor) = 1_587_301_587_301.
        assert_eq!(
            keeper.cash(BookId(1)).unwrap(),
            10_000_000_000_000_000 + 1_587_301_587_301
        );
    }

    #[test]
    fn positions_for_book_covers_the_whole_row_in_universe_order() {
        let mut keeper = PositionKeeper::new(
            &[BookId(1), BookId(2)],
            &[InstrumentId(1001), InstrumentId(1002), InstrumentId(1003)],
        );
        keeper
            .apply_fill(
                BookId(1),
                InstrumentId(1002),
                Side::Buy,
                500,
                100_000_000_000,
            )
            .unwrap();

        let row: Vec<_> = keeper.positions_for_book(BookId(1)).unwrap().collect();

        assert_eq!(row.len(), 3);
        assert_eq!(
            *row.first().unwrap(),
            (InstrumentId(1001), Position::default())
        );
        let mid = row.get(1).unwrap();
        assert_eq!(mid.0, InstrumentId(1002));
        assert_eq!(mid.1.net_qty_e2, 500);
        assert_eq!(
            *row.get(2).unwrap(),
            (InstrumentId(1003), Position::default())
        );
    }
}
