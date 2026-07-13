//! Live price book for a fixed instrument universe. Hot path: `ingest` is O(1)
//! and allocation-free once `new` has preallocated (delta-one/CLAUDE.md hot-path
//! contract).

use std::collections::HashMap;

use crate::feed::FeedTick;
use crate::ids::InstrumentId;

/// Latest bid/ask/last for one instrument.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Quote {
    /// Best bid price, fixed-point ×10⁹.
    pub bid_px_e9: i64,
    /// Best ask price, fixed-point ×10⁹.
    pub ask_px_e9: i64,
    /// Last/reference price, fixed-point ×10⁹.
    pub last_px_e9: i64,
    /// Exchange timestamp of the last update, nanoseconds since Unix epoch.
    /// Zero means "never ticked".
    pub exch_ts_ns: u64,
}

/// Preallocated price book, indexed by a dense instrument index built at
/// startup from the sparse `instrument_id`s (e.g. 1001, 2001, ...).
pub struct MarketData {
    index: HashMap<InstrumentId, usize>,
    ids: Vec<InstrumentId>,
    quotes: Vec<Quote>,
}

impl MarketData {
    /// Preallocate storage for a fixed instrument universe. Startup allocation
    /// only; not called on the hot path.
    #[must_use]
    pub fn new(instrument_ids: &[InstrumentId]) -> Self {
        let index = instrument_ids
            .iter()
            .enumerate()
            .map(|(i, id)| (*id, i))
            .collect();
        Self {
            index,
            ids: instrument_ids.to_vec(),
            quotes: vec![Quote::default(); instrument_ids.len()],
        }
    }

    /// Apply a feed tick to the price book. O(1), no allocation. Returns
    /// `false` if `tick.instrument_id` is outside the configured universe.
    pub fn ingest(&mut self, tick: &FeedTick) -> bool {
        let Some(&i) = self.index.get(&tick.instrument_id) else {
            return false;
        };
        let Some(quote) = self.quotes.get_mut(i) else {
            return false;
        };
        quote.bid_px_e9 = tick.bid_px_e9;
        quote.ask_px_e9 = tick.ask_px_e9;
        quote.last_px_e9 = tick.last_px_e9;
        quote.exch_ts_ns = tick.exch_ts_ns;
        true
    }

    /// Current quote for an instrument, if it is part of the configured universe.
    #[must_use]
    pub fn quote(&self, instrument_id: InstrumentId) -> Option<Quote> {
        let &i = self.index.get(&instrument_id)?;
        self.quotes.get(i).copied()
    }

    /// Iterate all configured instruments in universe order with their current quote.
    pub fn iter(&self) -> impl Iterator<Item = (InstrumentId, Quote)> + '_ {
        self.ids.iter().copied().zip(self.quotes.iter().copied())
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)] // tests: unwrap_used/expect_used are hot-path-only bans (delta-one/CLAUDE.md)
mod tests {
    use super::*;

    #[test]
    fn ingest_updates_known_instrument() {
        let mut md = MarketData::new(&[InstrumentId(1001), InstrumentId(2001)]);
        let updated = md.ingest(&FeedTick {
            instrument_id: InstrumentId(1001),
            bid_px_e9: 187_500_000_000,
            ask_px_e9: 187_520_000_000,
            last_px_e9: 187_510_000_000,
            exch_ts_ns: 42,
        });
        assert!(updated);
        let q = md.quote(InstrumentId(1001)).unwrap();
        assert_eq!(q.bid_px_e9, 187_500_000_000);
        assert_eq!(q.exch_ts_ns, 42);
        assert_eq!(md.quote(InstrumentId(2001)).unwrap(), Quote::default());
    }

    #[test]
    fn ingest_ignores_unknown_instrument() {
        let mut md = MarketData::new(&[InstrumentId(1001)]);
        let updated = md.ingest(&FeedTick {
            instrument_id: InstrumentId(9999),
            bid_px_e9: 1,
            ask_px_e9: 1,
            last_px_e9: 1,
            exch_ts_ns: 1,
        });
        assert!(!updated);
        assert_eq!(md.quote(InstrumentId(9999)), None);
    }
}
