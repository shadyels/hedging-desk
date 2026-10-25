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
    let script_path = out_path.with_extension("script");
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
        .arg("--script-out")
        .arg(&script_path)
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
    // EXACTLY these 16 ticks (14 quotes, 1 gap, 1 dividend; the 6 non-feed
    // entries -- exo_book_event, tracker_flow, 4 expects -- are skipped),
    // field by field. This one assertion covers the whole cross-crate
    // contract: YAML parse, symbol->id resolution, decimal->e9 conversion,
    // `gap`'s bid=ask=last, last-quote carry-forward into the dividend arm,
    // the ex-div drop, dividend field placement, tick-file line format, and
    // the integer round-trip through the file.
    let tick = |at_ms: u64, bid_px_e9: i64, ask_px_e9: i64, div_per_share_e9: i64| FeedTick {
        instrument_id: InstrumentId(1001),
        bid_px_e9,
        ask_px_e9,
        last_px_e9: (bid_px_e9 + ask_px_e9) / 2,
        exch_ts_ns: at_ms * 1_000_000,
        div_per_share_e9,
    };
    let pre_gap = |at_ms| tick(at_ms, 187_500_000_000, 187_520_000_000, 0);
    let post_gap = |at_ms| tick(at_ms, 176_000_000_000, 176_000_000_000, 0);
    let ex_div = |at_ms| tick(at_ms, 175_750_000_000, 175_750_000_000, 0);
    let expected = vec![
        pre_gap(0),
        pre_gap(2_000),
        pre_gap(4_000),
        pre_gap(6_000),
        pre_gap(8_000),
        pre_gap(10_000),
        post_gap(12_000), // the gap
        post_gap(14_000),
        post_gap(16_000),
        post_gap(18_000),
        post_gap(20_000),
        post_gap(22_000),
        tick(25_000, 175_750_000_000, 175_750_000_000, 250_000_000), // the dividend
        ex_div(26_000),
        ex_div(28_000),
        ex_div(30_000),
    ];
    let ticks = d1::tickfile::load(&out_path).expect("load emitted tick file");
    assert_eq!(ticks, expected);

    // The harness script: ids resolved, quantities in qty_e2, YAML order.
    let script = std::fs::read_to_string(&script_path).expect("read script");
    assert_eq!(
        script.lines().collect::<Vec<_>>(),
        [
            "10000 book_event 3",
            "11000 tracker_flow 1 1001 1000000 1000000 index redemption: sell 10,000 AAPL, held for internal liquidity within band",
            "12000 action gap",
            "24000 expect exo_targets_republished",
            "24000 expect internal_cross",
            "24000 expect external_residual_order",
            "25000 action dividend",
            "30000 expect tracker_analytics",
        ]
    );

    let _ = std::fs::remove_file(&out_path);
    let _ = std::fs::remove_file(&script_path);

    // 3. Rescale exch_ts_ns by 1/1000 (deliberate: keeps this test at ~30ms
    // of real time instead of ~30s) and drive the real pacer into a
    // real `rtrb` ring. The pacer is proven by ORDER plus non-blocking
    // completion -- NEVER by asserting wall-clock duration, which would be
    // a flaky test. `run_tick_file_producer` blocks for the whole
    // timeline, which is fine to do on the test thread before draining:
    // the ring (capacity `d1::RING_CAPACITY`, 64) easily holds all 16 ticks.
    let rescaled: Vec<FeedTick> = ticks
        .iter()
        .map(|t| FeedTick {
            exch_ts_ns: t.exch_ts_ns / 1_000,
            ..*t
        })
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
