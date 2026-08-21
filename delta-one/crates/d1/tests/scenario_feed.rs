//! Cross-crate proof that `sim --mode emit-ticks` output feeds `d1`'s real
//! feed ring: `sim` expands `sim/scenarios/tracker-flow.yaml` into a tick
//! file, `d1::tickfile::load` parses it back into `FeedTick`s, and
//! `d1::feed::run_tick_file_producer` paces them onto a real `rtrb` ring --
//! the transport `d1::spawn`'s `feed_ticks` parameter drives (see
//! `crates/d1/src/lib.rs::spawn`'s doc comment). NOT `#[ignore]`d: no
//! Docker, no NATS, no Kafka -- runs under plain `cargo test`, same
//! non-ignored real-subprocess pattern as
//! `fix_round_trip.rs::spawn_sim_acceptor`.
#![allow(clippy::unwrap_used, clippy::expect_used)] // integration test, not hot-path code (see crates/d1/src/config.rs's carve-out)

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::AtomicBool;

use d1_core::{FeedTick, InstrumentId};

/// `crates/d1` -> `crates` -> `delta-one`, same helper shape as
/// `fix_round_trip.rs::workspace_root`.
fn workspace_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("crates dir")
        .parent()
        .expect("delta-one dir")
        .to_path_buf()
}

#[test]
fn tracker_flow_scenario_feeds_the_feed_ring() {
    // `std::env::temp_dir()` is world-writable, so a fixed filename there is
    // both a collision risk (two concurrent `cargo test` runs racing the
    // same path) and a symlink-planting risk (`std::fs::write` below follows
    // a pre-planted symlink) -- `std::process::id()` makes the path unique
    // per test process, closing both. `std::fs::write` inside `emit::run`
    // still overwrites unconditionally, so this is overwrite-safe across
    // repeated runs of the SAME process, same as before.
    let out_path = std::env::temp_dir().join(format!(
        "d1-scenario-feed-test-tracker-flow-{}.ticks",
        std::process::id()
    ));

    // 1. Expand the flagship scenario into a tick file via a real `sim`
    // subprocess. `stdout(Stdio::null())` so `cargo run`'s `Compiling`/
    // `Finished` lines and `expand`'s `eprintln!` skip-summary don't bypass
    // the test harness's output capture on every `cargo test` run -- same
    // treatment as `fix_round_trip.rs::spawn_sim_acceptor`. `stderr` stays
    // inherited (also matching that helper) so a real failure is still
    // visible.
    let status = Command::new(env!("CARGO"))
        .args([
            "run",
            "--quiet",
            "-p",
            "sim",
            "--",
            "--mode",
            "emit-ticks",
            "--scenario",
            "../sim/scenarios/tracker-flow.yaml",
            "--out",
        ])
        .arg(&out_path)
        .current_dir(workspace_root())
        .stdout(Stdio::null())
        .stderr(Stdio::inherit())
        .status()
        .expect("run sim --mode emit-ticks");
    assert!(
        status.success(),
        "sim --mode emit-ticks exited with {status:?}"
    );

    // 2. Parse the emitted file back through `d1`'s own loader and assert
    // EXACTLY these 3 ticks, field by field. This one assertion covers the
    // whole cross-crate contract: YAML parse, symbol->id resolution,
    // decimal->e9 conversion, `gap`'s bid=ask=last, last-quote
    // carry-forward into the dividend arm, the ex-div drop, dividend field
    // placement, tick-file line format, and the integer round-trip through
    // the file.
    let ticks = d1::tickfile::load(&out_path).expect("load emitted tick file");
    assert_eq!(
        ticks,
        vec![
            FeedTick {
                instrument_id: InstrumentId(1001),
                bid_px_e9: 187_500_000_000,
                ask_px_e9: 187_520_000_000,
                last_px_e9: 187_510_000_000,
                exch_ts_ns: 0,
                div_per_share_e9: 0,
            },
            FeedTick {
                instrument_id: InstrumentId(1001),
                bid_px_e9: 176_000_000_000,
                ask_px_e9: 176_000_000_000,
                last_px_e9: 176_000_000_000,
                exch_ts_ns: 5_000_000_000,
                div_per_share_e9: 0,
            },
            FeedTick {
                instrument_id: InstrumentId(1001),
                bid_px_e9: 175_750_000_000,
                ask_px_e9: 175_750_000_000,
                last_px_e9: 175_750_000_000,
                exch_ts_ns: 9_000_000_000,
                div_per_share_e9: 250_000_000,
            },
        ]
    );

    let _ = std::fs::remove_file(&out_path);

    // 3. Rescale exch_ts_ns to 0/5ms/9ms (deliberate: keeps this test at
    // ~9ms of real time instead of ~9s) and drive the real pacer into a
    // real `rtrb` ring. The pacer is proven by ORDER plus non-blocking
    // completion -- NEVER by asserting wall-clock duration, which would be
    // a flaky test. `run_tick_file_producer` blocks for the whole
    // timeline, which is fine to do on the test thread before draining:
    // the ring (capacity `d1::RING_CAPACITY`, 64) easily holds all 3 ticks.
    let rescaled: Vec<FeedTick> = ticks
        .iter()
        .zip([0u64, 5_000_000, 9_000_000])
        .map(|(t, exch_ts_ns)| FeedTick { exch_ts_ns, ..*t })
        .collect();

    let (feed_tx, mut feed_rx) = rtrb::RingBuffer::<FeedTick>::new(d1::RING_CAPACITY);
    let shutdown = AtomicBool::new(false);
    d1::feed::run_tick_file_producer(&rescaled, feed_tx, &shutdown);

    let mut drained = Vec::new();
    while let Ok(tick) = feed_rx.pop() {
        drained.push(tick);
    }
    assert_eq!(drained, rescaled);
}
