//! Feed-ingest producer threads -- the ring + producer thread deferred from
//! Slice 2 (`delta-one/CLAUDE.md`'s M2 status note), plus the scenario
//! tick-file transport added later. Two producers, chosen by `d1::spawn`'s
//! `feed_ticks` parameter (`crates/d1/src/lib.rs::FeedSource`):
//! [`run_feed_producer`], a fixed synthetic price walk, and
//! [`run_tick_file_producer`], which replays a pre-parsed scenario tick file
//! (`crates/d1/src/tickfile.rs` is the file format; `crates/sim/src/emit.rs`
//! is the writer) paced to real time.
//!
//! ponytail: `run_feed_producer`'s walk remains the only thing standing in
//! for a *live* market-data transport -- `protocol/nats-subjects.md`'s
//! `sim.md.<instrument>` is UI-display-only, not D1's feed boundary, and no
//! real transport exists yet. The tick-file producer is a scripted-scenario
//! transport, not a live one either: it replays a fixed, pre-computed
//! timeline, not a feed a real venue could push updates onto. Both exist to
//! prove the ring/thread wiring actually moves ticks across the boundary
//! into the core (`run_core`'s `MarketData::ingest` drain); replace
//! `run_feed_producer` with a real feed producer when a live transport
//! lands.

use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::{Duration, Instant};

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

            if !push_blocking(&mut feed_tx, tick, shutdown) {
                return;
            }

            *px_e9 += drift_e9;
        }

        exch_ts_ns += TICK_INTERVAL.as_nanos() as u64;
        thread::sleep(TICK_INTERVAL);
    }
}

/// Push `tick`, retrying on a full ring at `PUSH_POLL_INTERVAL`. Returns
/// `false` if `shutdown` was observed before the push landed. Shared by
/// [`run_feed_producer`] and [`run_tick_file_producer`] -- both producers
/// must back off identically on a full ring and honour shutdown identically
/// while doing so.
fn push_blocking(
    feed_tx: &mut rtrb::Producer<FeedTick>,
    tick: FeedTick,
    shutdown: &AtomicBool,
) -> bool {
    let mut pending = Some(tick);
    while let Some(next) = pending.take() {
        if shutdown.load(Ordering::Relaxed) {
            return false;
        }
        match feed_tx.push(next) {
            Ok(()) => {}
            Err(rtrb::PushError::Full(returned)) => {
                pending = Some(returned);
                thread::sleep(PUSH_POLL_INTERVAL);
            }
        }
    }
    true
}

/// Replay `ticks` onto `feed_tx`, pacing each push to a real-time deadline
/// derived from the tick's `exch_ts_ns` (scenario-relative nanoseconds) --
/// the `sim`-generated counterpart to `run_feed_producer`'s synthetic walk
/// (`crates/d1/src/tickfile.rs` is the file format; `crates/sim/src/emit.rs`
/// is the writer).
///
/// Each tick's deadline is
/// `start.checked_add(Duration::from_nanos(tick.exch_ts_ns))`, where `start`
/// is ONE `Instant::now()` captured at entry -- an ABSOLUTE deadline, not a
/// per-gap delta accumulated tick over tick, so pacing error from
/// retries/scheduling jitter can never compound across the timeline.
/// `checked_add`, never bare `+` (`impl Add<Duration> for Instant` panics on
/// overflow internally, which would abort the process under
/// `panic = "abort"`): `None` means this platform's monotonic clock cannot
/// represent the deadline (`exch_ts_ns` near `u64::MAX`, ~584 years out) --
/// logged once to stderr naming the offending tick, and the producer returns
/// without pushing it or any later tick, rather than panicking.
///
/// Waits for each deadline in `PUSH_POLL_INTERVAL` chunks via
/// `Instant::checked_duration_since` (never bare `Instant` subtraction,
/// which panics on underflow), re-checking `shutdown` every chunk so a
/// Ctrl-C signalled mid-gap (e.g. `tracker-flow.yaml`'s ~4s gap between its
/// `gap` and `dividend` ticks) is honoured within about one
/// `PUSH_POLL_INTERVAL`, not at the next tick's deadline. Returns early if
/// `shutdown` is set, either while waiting or while pushing.
///
/// After the last tick, prints one line and returns -- the process stays up
/// (feed idle) until the caller signals shutdown; no phantom terminal tick
/// is synthesized to pad the timeline.
pub fn run_tick_file_producer(
    ticks: &[FeedTick],
    mut feed_tx: rtrb::Producer<FeedTick>,
    shutdown: &AtomicBool,
) {
    let start = Instant::now();

    for &tick in ticks {
        let Some(deadline) = start.checked_add(Duration::from_nanos(tick.exch_ts_ns)) else {
            eprintln!(
                "d1: tick deadline unrepresentable on this platform's monotonic clock, dropping remaining ticks from instrument={:?} exch_ts_ns={}",
                tick.instrument_id, tick.exch_ts_ns
            );
            return;
        };
        loop {
            if shutdown.load(Ordering::Relaxed) {
                return;
            }
            let Some(remaining) = deadline.checked_duration_since(Instant::now()) else {
                break;
            };
            if remaining.is_zero() {
                break;
            }
            thread::sleep(remaining.min(PUSH_POLL_INTERVAL));
        }

        if !push_blocking(&mut feed_tx, tick, shutdown) {
            return;
        }
    }

    println!("d1: tick file exhausted ({} ticks), feed idle", ticks.len());
}
