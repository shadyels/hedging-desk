//! Synthetic feed-ingest producer thread -- the ring + producer thread
//! deferred from Slice 2 (`delta-one/CLAUDE.md`'s M2 status note).
//!
//! ponytail: fixed synthetic price walk, no real market-data transport --
//! `protocol/nats-subjects.md`'s `sim.md.<instrument>` is UI-display-only,
//! not D1's feed boundary, and no such transport exists yet. This exists
//! purely to prove the deferred ring/thread wiring actually moves ticks
//! across the boundary into the core (`run_core`'s `MarketData::ingest`
//! drain); replace with a real feed producer when one lands.

use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::Duration;

use d1_core::{FeedTick, InstrumentId};

const TICK_INTERVAL: Duration = Duration::from_millis(500);
const PUSH_POLL_INTERVAL: Duration = Duration::from_millis(5);
/// 150.00, arbitrary demo price. `pub(crate)` so `crates/d1/src/lib.rs::run_core`
/// can prime `MarketData` synchronously at t=0 with the same starting price
/// this producer ticks from (MEDIUM 2: the arrival mid used to price the
/// first netting cycle's crosses must not depend on whether the feed
/// thread's first burst has landed before a target is drained).
pub(crate) const STARTING_PX_E9: i64 = 150_000_000_000;
const SPREAD_E9: i64 = 10_000_000; // 0.01
/// Live-mode per-tick drift, deterministic not random. The golden-file
/// deterministic path (`crates/d1/src/lib.rs::spawn`) passes `0` instead,
/// pinning every tick's mid at `STARTING_PX_E9` regardless of tick count.
pub(crate) const DRIFT_E9: i64 = 1_000_000; // 0.001/tick
/// Demo dividend schedule: fires once, the moment the synthetic
/// `exch_ts_ns` clock first crosses this boundary -- mirrors
/// `sim/scenarios/tracker-flow.yaml`'s `action: dividend` on AAPL @9000ms.
pub(crate) const DIVIDEND_AT_NS: u64 = 9_000_000_000;
/// AAPL's demo dividend amount, `_e9` fixed point (0.25/share).
pub(crate) const DIVIDEND_PER_SHARE_E9: i64 = 250_000_000;

/// Emit one `FeedTick` per instrument in `instruments` onto `feed_tx` every
/// `TICK_INTERVAL` until `shutdown` is set. `drift_e9` is added to each
/// instrument's running price after every tick -- `DRIFT_E9` live, `0` for
/// the deterministic golden-file path.
///
/// Every instrument in the keeper universe is ticked, not just the CLI
/// startup order's: `d1_core::MarketData`'s arrival mid is what
/// `cycle::book_cross` prices an internal cross at, so an instrument that
/// never receives a tick books its crosses at `ref_px_e9 = 0`. One thread
/// covers all of them because `feed_tx` is an `rtrb` SPSC producer (ADR-013)
/// -- a thread per instrument would need a ring per instrument.
pub fn run_feed_producer(
    instruments: &[InstrumentId],
    mut feed_tx: rtrb::Producer<FeedTick>,
    drift_e9: i64,
    shutdown: &AtomicBool,
) {
    let mut last_px_e9 = vec![STARTING_PX_E9; instruments.len()];
    let mut exch_ts_ns = 0u64;
    // Latched so the demo dividend fires exactly once per session, on the
    // first tick whose clock has reached `DIVIDEND_AT_NS`.
    let mut dividend_fired = false;

    while !shutdown.load(Ordering::Relaxed) {
        for (i, (instrument, px_e9)) in instruments.iter().zip(last_px_e9.iter_mut()).enumerate() {
            // Rides the FIRST instrument's tick (AAPL in universe.json
            // order) the moment the clock first crosses `DIVIDEND_AT_NS` --
            // see `FeedTick::div_per_share_e9`'s doc comment for why this
            // travels on the tick stream instead of its own ring. This
            // slice only puts the field on the wire; crediting the cash
            // (`PositionKeeper::credit_dividend`) is `run_core`'s job
            // (Slice 2). The running price is dropped by the same amount on
            // this SAME tick (M6 remediation) -- without this, the book
            // would book the dividend cash AND keep the pre-div price,
            // creating value out of nothing; this is what makes this
            // field's doc comment's "ex-price move" claim true.
            let div_per_share_e9 = if i == 0 && !dividend_fired && exch_ts_ns >= DIVIDEND_AT_NS {
                dividend_fired = true;
                *px_e9 -= DIVIDEND_PER_SHARE_E9;
                DIVIDEND_PER_SHARE_E9
            } else {
                0
            };
            let tick = FeedTick {
                instrument_id: *instrument,
                bid_px_e9: *px_e9 - SPREAD_E9,
                ask_px_e9: *px_e9 + SPREAD_E9,
                last_px_e9: *px_e9,
                exch_ts_ns,
                div_per_share_e9,
            };

            let mut pending = Some(tick);
            while let Some(next) = pending.take() {
                if shutdown.load(Ordering::Relaxed) {
                    return;
                }
                match feed_tx.push(next) {
                    Ok(()) => {}
                    Err(rtrb::PushError::Full(returned)) => {
                        pending = Some(returned);
                        thread::sleep(PUSH_POLL_INTERVAL);
                    }
                }
            }

            *px_e9 += drift_e9;
        }

        exch_ts_ns += TICK_INTERVAL.as_nanos() as u64;
        thread::sleep(TICK_INTERVAL);
    }
}
