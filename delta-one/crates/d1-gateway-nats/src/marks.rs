//! Mark throttle (ADR-014 §2): the core pushes a `MarkUpdate` on every
//! in-universe tick; this gateway-side state decides what actually goes on
//! `d1.marks.<instrument>`. Pure, with an injected `Instant`.

use std::collections::BTreeMap;
use std::time::{Duration, Instant};

use d1_core::InstrumentId;

use crate::MarkUpdate;

/// Minimum spacing between two change-driven publishes of one instrument.
pub const MIN_PUBLISH_INTERVAL: Duration = Duration::from_millis(250);

struct Entry {
    latest: MarkUpdate,
    published_mid_px_e9: Option<i64>,
    last_published: Option<Instant>,
}

/// Coalesces to the latest update per instrument.
#[derive(Default)]
pub struct MarkThrottle {
    by_instrument: BTreeMap<InstrumentId, Entry>,
}

impl MarkThrottle {
    /// An empty throttle: nothing has ever ticked.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Record the latest update for its instrument (older ones are dropped).
    pub fn on_update(&mut self, update: MarkUpdate) {
        self.by_instrument
            .entry(update.instrument)
            .and_modify(|e| e.latest = update)
            .or_insert(Entry {
                latest: update,
                published_mid_px_e9: None,
                last_published: None,
            });
    }

    /// Updates to publish now: the mid differs from the last published mid and
    /// at least 250 ms have passed since that instrument's last publish. Call
    /// every gateway iteration: a change suppressed by the window is published
    /// the first call after the window ends (trailing edge).
    pub fn due(&mut self, now: Instant) -> Vec<MarkUpdate> {
        let mut out = Vec::new();
        for entry in self.by_instrument.values_mut() {
            let changed = entry.published_mid_px_e9 != Some(entry.latest.mid_px_e9);
            let window_open = entry
                .last_published
                .is_none_or(|t| now.saturating_duration_since(t) >= MIN_PUBLISH_INTERVAL);
            if changed && window_open {
                entry.published_mid_px_e9 = Some(entry.latest.mid_px_e9);
                entry.last_published = Some(now);
                out.push(entry.latest);
            }
        }
        out
    }

    /// Every instrument that has ever ticked, at its latest mid: the 1 s
    /// keepalive, so a late subscriber converges within a second.
    pub fn keepalive(&mut self, now: Instant) -> Vec<MarkUpdate> {
        self.by_instrument
            .values_mut()
            .map(|entry| {
                entry.published_mid_px_e9 = Some(entry.latest.mid_px_e9);
                entry.last_published = Some(now);
                entry.latest
            })
            .collect()
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)] // tests: unwrap_used/expect_used are hot-path-only bans (delta-one/CLAUDE.md)
mod tests {
    use super::*;

    const AAPL: InstrumentId = InstrumentId(1001);
    const MSFT: InstrumentId = InstrumentId(1002);

    fn mark(instrument: InstrumentId, mid: i64) -> MarkUpdate {
        MarkUpdate {
            instrument,
            mid_px_e9: mid,
            feed_ts_ns: 0,
        }
    }

    #[test]
    fn first_update_publishes_immediately_then_unchanged_mid_does_not() {
        let mut t = MarkThrottle::new();
        let t0 = Instant::now();
        t.on_update(mark(AAPL, 100));
        assert_eq!(t.due(t0).len(), 1);
        t.on_update(mark(AAPL, 100)); // same mid
        assert!(t.due(t0 + Duration::from_secs(5)).is_empty());
    }

    #[test]
    fn change_inside_the_window_is_published_on_the_trailing_edge() {
        let mut t = MarkThrottle::new();
        let t0 = Instant::now();
        t.on_update(mark(AAPL, 100));
        assert_eq!(t.due(t0).len(), 1);
        t.on_update(mark(AAPL, 101));
        t.on_update(mark(AAPL, 102)); // coalesced: 101 is never published
        assert!(t.due(t0 + Duration::from_millis(249)).is_empty());
        let due = t.due(t0 + Duration::from_millis(250));
        assert_eq!(due.len(), 1);
        assert_eq!(due.first().unwrap().mid_px_e9, 102);
        assert!(t.due(t0 + Duration::from_millis(600)).is_empty());
    }

    #[test]
    fn instruments_throttle_independently() {
        let mut t = MarkThrottle::new();
        let t0 = Instant::now();
        t.on_update(mark(AAPL, 100));
        t.due(t0);
        t.on_update(mark(AAPL, 101));
        t.on_update(mark(MSFT, 50));
        let due = t.due(t0 + Duration::from_millis(10));
        assert_eq!(due.len(), 1); // MSFT's first; AAPL still inside its window
        assert_eq!(due.first().unwrap().instrument, MSFT);
    }

    #[test]
    fn keepalive_republishes_every_ticked_instrument_and_only_those() {
        let mut t = MarkThrottle::new();
        let t0 = Instant::now();
        assert!(t.keepalive(t0).is_empty()); // never ticked: no mark
        t.on_update(mark(AAPL, 100));
        t.due(t0);
        let ka = t.keepalive(t0 + Duration::from_secs(1));
        assert_eq!(ka.len(), 1);
        // The keepalive counts as a publish: no immediate duplicate.
        assert!(t.due(t0 + Duration::from_secs(1)).is_empty());
    }
}
