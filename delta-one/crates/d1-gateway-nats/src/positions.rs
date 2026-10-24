//! Position mirror (ADR-014 §7): the gateway-side copy of the core's
//! absolute per-(book, instrument) `PositionUpdate`s, from which
//! `PositionSnapshot`s are built. Pure.

use std::collections::{BTreeMap, BTreeSet};

use d1_core::BookId;

use crate::PositionUpdate;

/// Latest absolute position per book and instrument, plus which books changed
/// since the last snapshot.
#[derive(Default)]
pub struct PositionMirror {
    books: BTreeMap<BookId, BTreeMap<d1_core::InstrumentId, PositionUpdate>>,
    dirty: BTreeSet<BookId>,
}

impl PositionMirror {
    /// An empty mirror.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Apply one absolute update. A flat, nothing-inflight line is removed
    /// (a missing line means 0); the book is marked changed only if the
    /// mirror actually changed, so a repeated update is not re-published.
    pub fn apply(&mut self, update: PositionUpdate) {
        let lines = self.books.entry(update.book).or_default();
        let changed = if update.qty_e2 != 0 || update.inflight_qty_e2 != 0 {
            lines.insert(update.instrument, update) != Some(update)
        } else {
            lines.remove(&update.instrument).is_some()
        };
        if changed {
            self.dirty.insert(update.book);
        }
    }

    /// The non-flat lines of `book`, in instrument order (empty for a flat or
    /// unknown book).
    #[must_use]
    pub fn lines(&self, book: BookId) -> Vec<PositionUpdate> {
        self.books
            .get(&book)
            .map(|lines| lines.values().copied().collect())
            .unwrap_or_default()
    }

    /// Books that changed since the last call; clears the set.
    pub fn take_dirty(&mut self) -> Vec<BookId> {
        std::mem::take(&mut self.dirty).into_iter().collect()
    }

    /// Forget pending changes (every book was just published).
    pub fn clear_dirty(&mut self) {
        self.dirty.clear();
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)] // tests: unwrap_used/expect_used are hot-path-only bans (delta-one/CLAUDE.md)
mod tests {
    use d1_core::InstrumentId;

    use super::*;

    fn up(book: u32, instr: u32, qty: i64, inflight: i64) -> PositionUpdate {
        PositionUpdate {
            book: BookId(book),
            instrument: InstrumentId(instr),
            qty_e2: qty,
            avg_px_e9: 150,
            inflight_qty_e2: inflight,
        }
    }

    #[test]
    fn change_marks_dirty_once_and_repeat_does_not() {
        let mut m = PositionMirror::new();
        m.apply(up(1, 1001, 100, 0));
        assert_eq!(m.take_dirty(), vec![BookId(1)]);
        m.apply(up(1, 1001, 100, 0)); // absolute and identical
        assert!(m.take_dirty().is_empty());
        m.apply(up(1, 1001, 100, 50)); // inflight moved
        assert_eq!(m.take_dirty(), vec![BookId(1)]);
    }

    #[test]
    fn flat_lines_are_omitted_and_removal_is_a_change() {
        let mut m = PositionMirror::new();
        m.apply(up(1, 1001, 100, 0));
        m.apply(up(1, 1002, 0, 0)); // flat, never seen: no line, no change
        m.take_dirty();
        assert_eq!(m.lines(BookId(1)).len(), 1);
        m.apply(up(1, 1001, 0, 0));
        assert!(m.lines(BookId(1)).is_empty());
        assert_eq!(m.take_dirty(), vec![BookId(1)]);
        assert!(m.lines(BookId(7)).is_empty()); // unknown book: empty, not an error
    }

    #[test]
    fn books_are_isolated_and_each_line_belongs_to_its_book() {
        let mut m = PositionMirror::new();
        m.apply(up(1, 1001, 100, 0));
        m.apply(up(2, 1001, -40, 0));
        assert_eq!(m.take_dirty(), vec![BookId(1), BookId(2)]);
        assert!(m.lines(BookId(1)).iter().all(|l| l.book == BookId(1)));
        assert_eq!(m.lines(BookId(1)).first().unwrap().qty_e2, 100);
        assert_eq!(m.lines(BookId(2)).first().unwrap().qty_e2, -40);
        m.apply(up(2, 1001, -50, 0));
        assert_eq!(m.take_dirty(), vec![BookId(2)]);
        m.apply(up(1, 1001, 101, 0));
        m.clear_dirty();
        assert!(m.take_dirty().is_empty());
    }
}
