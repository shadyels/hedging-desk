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
const STARTING_PX_E9: i64 = 150_000_000_000; // 150.00, arbitrary demo price
const SPREAD_E9: i64 = 10_000_000; // 0.01
const DRIFT_E9: i64 = 1_000_000; // 0.001/tick, deterministic not random

/// Emit one `FeedTick` for `instrument` onto `feed_tx` every `TICK_INTERVAL`
/// until `shutdown` is set.
pub fn run_feed_producer(
    instrument: InstrumentId,
    mut feed_tx: rtrb::Producer<FeedTick>,
    shutdown: &AtomicBool,
) {
    let mut last_px_e9 = STARTING_PX_E9;
    let mut exch_ts_ns = 0u64;

    while !shutdown.load(Ordering::Relaxed) {
        let tick = FeedTick {
            instrument_id: instrument,
            bid_px_e9: last_px_e9 - SPREAD_E9,
            ask_px_e9: last_px_e9 + SPREAD_E9,
            last_px_e9,
            exch_ts_ns,
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

        last_px_e9 += DRIFT_E9;
        exch_ts_ns += TICK_INTERVAL.as_nanos() as u64;
        thread::sleep(TICK_INTERVAL);
    }
}
