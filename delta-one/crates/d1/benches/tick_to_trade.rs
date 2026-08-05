//! Composed tick-to-trade (T2T) bench: measures the real ADR-004 §1
//! sequence end to end -- feed ingest, ref-price read, netting/order-build
//! via `d1::cycle::NettingSession::on_target`, and order emit onto a real
//! `rtrb` ring -- instead of the four independently-benched stages in
//! `crates/d1-core/benches/hot_path.rs` summed together. Summing independent
//! stage medians is not a path p99: the queueing and cache effects between
//! stages are exactly what the tail measures, and they vanish when stages
//! are benched in isolation. This bench closes that gap (Phase 1 exit
//! criterion, ADR-004 §5).
//!
//! ## Exclusions from the timed region
//!
//! `OrderStore::place` (`lib.rs:623`) is deliberately EXCLUDED: in the live
//! loop it runs *after* the FIX-outbound-ring push (`lib.rs:618`), so the
//! emit timestamp for ADR-004's tick-to-order-emit budget is the ring push,
//! not the store insert.
//!
//! The cross-publication block at `lib.rs:571-604` -- the `println!` at
//! `lib.rs:572-576`, `posttrade::cross_events` plus two `push_posttrade`
//! calls per cross, and `cross_tx.push` -- is ALSO excluded, even though it
//! fires on every timed sample of this scenario in the live loop (this
//! scenario always books a cross). Not an accidental omission: ADR-004 §1
//! says outright, "Hot path is in-process only. EXO targets, UI commands,
//! Kafka, and even the NATS publish of execution reports are all *off* the
//! path, connected via bounded SPSC rings." The cross notice (NATS
//! `d1.crosses`) and post-trade events (Kafka) are exactly that class of
//! off-path publish, so this bench stops at the ring push that represents
//! the order-emit boundary and never calls into that block.
//!
//! The ingest-arm `println!` at `lib.rs:448-453` is likewise NOT reproduced
//! here, even though the live loop emits it on every quote-changing tick and
//! therefore on every sample of this scenario. Listing it explicitly matters
//! because the omission flatters the number: `cycle.rs:320`'s `println!` IS
//! inside this window (it lives inside `on_target`) while this one is not,
//! so the two demo log lines get inconsistent treatment. Both belong to the
//! same class of debt -- `run_core`'s demo-grade stdout logging, which
//! `delta-one/CLAUDE.md`'s hot-path rule #3 would have as a telemetry ring --
//! recorded together in `docs/PONYTAIL-DEBT.md`.
//!
//! This is a **composed-path** measurement, not a causally tick-triggered
//! one: the timed sequence is target-triggered (an EXO `Target` arriving),
//! with one feed tick ingested immediately beforehand purely to prime the
//! arrival-mid reference price `on_target` needs -- exactly what `run_core`
//! does before calling it (`lib.rs:445-569`).
//!
//! Runs under the `bench` profile, which inherits `release`
//! (`delta-one/Cargo.toml:38-39`) -- never run this on a debug build.
//!
//! ## stdout volume (until `cycle.rs:320`'s `println!` is removed)
//!
//! `cycle::on_target` unconditionally `println!`s once per minted parent
//! order (`cycle.rs:320-323`), and this scenario mints one on every timed
//! sample by design. With `HDR_SAMPLES = 100_000` plus criterion's warm-up
//! and measurement iterations (on the order of 10^6 total calls), and each
//! line rendering `ClOrdId`'s derived `Debug` over a `[u8; 20]`
//! (`d1-core/src/ids.rs:15-16`, roughly 200 bytes/line), one `just bench`
//! run of this file emits on the order of a few hundred MB of stdout. RUN
//! THIS BENCH WITH STDOUT REDIRECTED (to a file or `/dev/null`): the two HDR
//! report lines below (printed once, at the very end of the HDR loop) are
//! the deliverable, not the per-sample `println!` noise burying them. This
//! is deliberately NOT fixed by editing `cycle.rs` here -- that file is a
//! production file, out of scope for this bench, and its own module doc
//! comment already accepts `println!`-driven, off-hot-path logging for a
//! single demo session.
//!
//! Numbers this bench prints are UNPINNED: macOS/Apple Silicon does not
//! honour thread-to-core affinity (the Mach `thread_policy_set` affinity tag
//! is a hint Apple Silicon ignores), so ADR-004 §5's "pinned cores" clause is
//! unmeetable on this dev platform -- no `core_affinity` dependency was
//! added rather than ship a silent no-op.
//!
//! ## Two reported series measure different shapes
//!
//! `hdr_report_batched`'s HDR lines are the ADR-004 §5 deliverable: one
//! `SampleState` is built, timed once, and torn down, per sample. Criterion's
//! `iter_batched_ref(..., BatchSize::SmallInput)` instead pre-builds
//! `iters / 10` `SampleState`s in a batch before timing them in a tight loop
//! -- different allocator and cache-locality behavior than the HDR loop's
//! build-time-teardown cadence. Do not expect the two series to agree with
//! each other; the criterion series is a relative regression signal (useful
//! for `cargo bench`'s own A/B diffing across commits), not a second HDR
//! measurement of the same per-sample shape.
#![allow(missing_docs)] // bench binary, not a public library API

use std::time::Instant;

use criterion::{BatchSize, Criterion, black_box, criterion_group, criterion_main};
use d1::RING_CAPACITY;
use d1::cycle::{CycleOutput, NettingSession};
use d1_core::{BookId, FeedTick, InstrumentId, MarketData, Order, PositionKeeper, Target};
use d1_netting::RefPxPolicy;
use d1_posttrade::Stamper;

const HDR_SAMPLES: u32 = 100_000;

/// Reference/tick price, fixed-point x10^9. Local to this bench --
/// `d1::feed::STARTING_PX_E9` is `pub(crate)` and unreachable from an
/// external bench crate.
const REF_PRICE_E9: i64 = 100_000_000_000;

const BOOK_A: BookId = BookId(1);
const BOOK_B: BookId = BookId(2);
const BOOK_C: BookId = BookId(3);
const INSTRUMENT: InstrumentId = InstrumentId(1);

/// The live keeper/market-data universe is `protocol/refdata/universe.json`'s
/// 5 books x 15 instruments (checked directly against the file: `books` has
/// 5 entries, `instruments` has 15). Sizing `PositionKeeper`/`MarketData`
/// and `NettingSession::targets` to match keeps `MarketData::ingest`'s map
/// lookup (`d1-core/src/market_data.rs`) and `on_target`'s `self.targets`
/// scan (`cycle.rs:198`) realistic instead of near-trivial -- a 3-book,
/// 1-instrument universe would bias the composed number optimistic.
const N_BOOKS: u32 = 5;
const N_INSTRUMENTS: u32 = 15;

/// Wide enough that book A's and B's own seeded targets never mint an
/// external order or register inflight on their own seeding calls
/// (`net_external_abs <= band_e2` suppresses the order entirely --
/// `d1-netting`'s band step, `crates/d1-netting/src/lib.rs`). That keeps
/// their demand un-consumed (position and inflight both stay 0), so it is
/// still there for book C's opposing target to cross against on the TIMED
/// call, instead of being fully absorbed by A/B's own already-inflight
/// orders before C ever arrives. Also used for the `N_INSTRUMENTS - 1`
/// filler targets below, for the same suppression reason.
const WIDE_BAND_E2: i64 = 10_000_000;

/// Everything one timed sample needs, built fresh per sample in UNTIMED
/// setup.
struct SampleState {
    session: NettingSession,
    keeper: PositionKeeper,
    market_data: MarketData,
    tx: rtrb::Producer<Order>,
    tick: FeedTick,
    target_c: Target,
}

/// UNTIMED per-sample setup. `on_target` mutates state (`session.parents`
/// grows a `ClOrdId` entry per external order, positions drift, the ring
/// fills), so three things must be rebuilt fresh every sample or they
/// contaminate the tail over `HDR_SAMPLES` iterations:
///
/// 1. `session.parents` (a `HashMap`) would grow unbounded -> rehash spikes
///    land exactly in p99.9.
/// 2. Positions would drift and the ring would eventually stop draining, so
///    the timed `push` would stop being a genuine success.
/// 3. `ClOrdId::from_seq`'s sequence would keep advancing.
///
/// Book A and book B's targets are seeded here via UNTIMED `on_target`
/// calls (wide band, see `WIDE_BAND_E2`) so the TIMED call -- book C's
/// opposing target arriving -- lands on warm state that produces both an
/// internal cross (A crosses against C) and a residual external order (the
/// realistic shape, matching the demo storyline; a single book's target in
/// isolation produces no cross and would under-measure). `N_INSTRUMENTS - 1`
/// filler targets (see below) additionally warm `session.targets` to
/// universe.json's real size, all UNTIMED and provably inert for the timed
/// cycle.
fn setup_sample() -> SampleState {
    let books: Vec<BookId> = (1..=N_BOOKS).map(BookId).collect();
    let instruments: Vec<InstrumentId> = (1..=N_INSTRUMENTS).map(InstrumentId).collect();
    let mut keeper = PositionKeeper::new(&books, &instruments);
    let market_data = MarketData::new(&instruments);
    // `Stamper::Wall` (the live path's `Uuid::now_v7()`,
    // `d1-posttrade/src/lib.rs:78-82`), deliberately NOT `Stamper::Fixed`:
    // the timed call books a cross, so `cycle.rs:103`'s `stamper.uuid()`
    // runs INSIDE the timed window -- whichever variant is used is what
    // gets measured. `Stamper`'s own doc explains why `Fixed` must never
    // stand in here: "a fixture is not a latency measurement"
    // (`d1-posttrade/src/lib.rs:50-58`). Nothing in this bench asserts on
    // `cross_id`, so determinism buys nothing, and using `Fixed` would
    // systematically understate T2T by one `now_v7` (clock read + RNG) per
    // sample versus what the live path actually pays.
    let mut session = NettingSession::new(RefPxPolicy::ArrivalMid, 1, Stamper::Wall);

    // Filler targets on the other N_INSTRUMENTS - 1 instruments, wide band
    // (mint no order, register no inflight): realistically populates
    // `session.targets`'s HashMap to universe.json's real size. Provably
    // inert for the timed cycle -- `on_target` filters every stored entry
    // with `if instrument != target.instrument { continue; }`
    // (`cycle.rs:199-201`), so these only lengthen the scan, never join the
    // netted demand for `INSTRUMENT`. `assert_scenario_nets` (called once,
    // UNTIMED, before any measurement) is the proof: it keeps passing with
    // these fillers present.
    for instrument_id in 2..=N_INSTRUMENTS {
        let filler = Target {
            book: BOOK_A,
            instrument: InstrumentId(instrument_id),
            target_qty_e2: 1_000,
            band_e2: WIDE_BAND_E2,
        };
        let _ = session.on_target(filler, &mut keeper, REF_PRICE_E9);
    }

    // Untimed seeding calls: warm `session.targets`/`session.parents`
    // before the timed call, never on the timed path themselves.
    let target_a = Target {
        book: BOOK_A,
        instrument: INSTRUMENT,
        target_qty_e2: 1_000_000,
        band_e2: WIDE_BAND_E2,
    };
    let target_b = Target {
        book: BOOK_B,
        instrument: INSTRUMENT,
        target_qty_e2: 400_000,
        band_e2: WIDE_BAND_E2,
    };
    let _ = session.on_target(target_a, &mut keeper, REF_PRICE_E9);
    let _ = session.on_target(target_b, &mut keeper, REF_PRICE_E9);

    // The timed Nth (third) book's target: opposing sign vs. A/B so
    // netting genuinely crosses (worked example, delta-one/CLAUDE.md: book
    // A target +N, book B target -M -> cross min(N,M), external residual).
    let target_c = Target {
        book: BOOK_C,
        instrument: INSTRUMENT,
        target_qty_e2: -700_000,
        band_e2: 0,
    };

    let tick = FeedTick {
        instrument_id: INSTRUMENT,
        bid_px_e9: REF_PRICE_E9,
        ask_px_e9: REF_PRICE_E9 + 10_000_000,
        last_px_e9: REF_PRICE_E9 + 5_000_000,
        exch_ts_ns: 1,
        div_per_share_e9: 0,
    };

    // Fresh ring every sample, so the timed emit-stage push below is a
    // genuine SPSC write, not one that immediately fails `Full`. The
    // consumer half is dropped right away -- `rtrb` has no disconnect
    // signal (delta-one/CLAUDE.md), so that does not affect the producer.
    let (tx, _rx) = rtrb::RingBuffer::<Order>::new(RING_CAPACITY);

    SampleState {
        session,
        keeper,
        market_data,
        tx,
        tick,
        target_c,
    }
}

/// UNTIMED, called once before any measurement (see `bench_tick_to_trade`):
/// fails loudly if the scenario this bench rests on ever degenerates to a
/// no-cross or no-residual shape -- e.g. a future change to `d1-netting`'s
/// band rule or `cycle.rs`'s demand computation. Without this, the bench
/// would keep printing plausible-looking numbers for a completely different
/// (and cheaper) code path with zero signal that the scenario broke.
// Bench code measures the hot path but isn't on it -- narrow expect_used
// carve-out (delta-one/CLAUDE.md grants any clearly marked non-hot module
// this) scoped to this one assertion function only, not the module-level
// allow.
#[allow(clippy::expect_used)]
fn assert_scenario_nets() {
    let mut probe = setup_sample();
    let out = probe
        .session
        .on_target(probe.target_c, &mut probe.keeper, REF_PRICE_E9)
        .expect("T2T scenario must net");
    assert_eq!(
        out.crosses_to_book.len(),
        1,
        "scenario must produce exactly one internal cross"
    );
    assert!(
        out.parent_order.is_some(),
        "scenario must produce an external residual order"
    );
}

/// TIMED region: the four ADR-004 §1 stages, in `run_core`'s live order
/// (`crates/d1/src/lib.rs:445-618`), stopping at the order-emit boundary --
/// see the module doc's "Exclusions from the timed region" section for what
/// (and why) is left out:
/// 1. feed ingest (`market_data.ingest`, `lib.rs:447`)
/// 2. ref-price read (`arrival_mid_px_e9`, `lib.rs:568`)
/// 3. position/risk check + netting + order build (`session.on_target`,
///    `lib.rs:569` -> `cycle.rs:168-335`)
/// 4. order emit (`fix_outbound_tx.push`, `lib.rs:618`)
///
/// Takes `&mut SampleState` (not by value -- same teardown reasoning as
/// before) AND returns `Option<CycleOutput>` rather than dropping it here:
/// `CycleOutput` owns a `Vec<CrossRecord>` (`cycle.rs:58-71`), and this
/// scenario's timed call always returns one populated entry, so dropping it
/// inline would put that `Vec`'s free back inside the timed window -- the
/// same defect class the `&mut SampleState` change fixed, one level down.
/// Callers must drop the returned value only AFTER reading elapsed time; see
/// `hdr_report_batched`'s loop (explicit `drop(out)`) and
/// `bench_tick_to_trade`'s `iter_batched_ref` call, which gets this for
/// free: criterion 0.5.1 `bencher.rs:355-361` collects batched routine
/// outputs and drops them only after `Measurement::end` fires.
fn run_t2t(state: &mut SampleState) -> Option<CycleOutput> {
    let _ = black_box(state.market_data.ingest(&state.tick));
    let ref_px_e9 = black_box(d1::arrival_mid_px_e9(
        &state.market_data,
        state.target_c.instrument,
    ));
    let output = state
        .session
        .on_target(state.target_c, &mut state.keeper, ref_px_e9)
        .ok()?;
    black_box(output.crosses_to_book.len());
    if let Some(order) = output.parent_order {
        black_box(state.tx.push(order).is_ok());
    }
    Some(output)
}

// Bench code measures the hot path but isn't on it -- the expect_used
// carve-out delta-one/CLAUDE.md grants any clearly marked non-hot module
// (mirrors `crates/d1-core/benches/hot_path.rs`).
//
// `op` takes `&mut SampleState` and returns `R`; `state` AND the returned
// `R` (see `run_t2t`'s `Option<CycleOutput>`) are dropped only AFTER
// `elapsed()` is read below -- teardown (freeing `session`'s two
// `HashMap`s, the keeper/market-data `Vec`s, the `rtrb` ring, and any
// `Vec`s owned by `R`) is real work but not part of ADR-004 §1's
// tick->order-emit sequence, exactly the same reasoning that excludes
// `setup` from the timed window. Criterion's own `iter_batched` would make
// the identical mistake with a by-value input -- its doc note (criterion
// 0.5.1 `bencher.rs:30-31`) says so explicitly: "If the setup value
// implements `Drop` and you don't want to include the `drop` time in the
// measurement, use `iter_batched_ref`" -- which is why `bench_tick_to_trade`
// below uses `iter_batched_ref`, not `iter_batched`.
//
// `state` is also `black_box`-ed both at construction and at the `op` call
// site: under the `bench` profile's `lto = "fat"` + `codegen-units = 1`
// (`delta-one/Cargo.toml:33-39`), `setup_sample` and `run_t2t` are fully
// inlinable/visible to LLVM at this call site, so nothing else marks
// `state` opaque between them -- criterion black-boxes its own batched
// inputs for the same reason (`bencher.rs:339`).
#[allow(clippy::expect_used)]
fn hdr_report_batched<R>(
    name: &str,
    mut setup: impl FnMut() -> SampleState,
    mut op: impl FnMut(&mut SampleState) -> R,
) {
    let mut hist = hdrhistogram::Histogram::<u64>::new(3).expect("valid histogram sigfigs");
    for _ in 0..HDR_SAMPLES {
        // UNTIMED: state construction is excluded from the measured window.
        let mut state = black_box(setup());
        let start = Instant::now();
        let out = op(black_box(&mut state));
        let elapsed_ns = start.elapsed().as_nanos() as u64;
        let _ = hist.record(elapsed_ns);
        // UNTIMED: teardown happens after the elapsed read, deliberately --
        // see the function doc comment above. Both `state` and `out` are
        // dropped here, not inline inside `op`.
        drop(state);
        drop(out);
    }
    println!(
        "{name}: p50={}ns p99={}ns p99.9={}ns",
        hist.value_at_quantile(0.50),
        hist.value_at_quantile(0.99),
        hist.value_at_quantile(0.999),
    );
    println!(
        "  NOTE: UNPINNED -- macOS/Apple Silicon does not honour thread-to-core affinity (the Mach `thread_policy_set` affinity tag is a hint Apple Silicon ignores), so ADR-004 §5's \"pinned cores\" clause is unmeetable on this dev platform; no `core_affinity` dependency was added rather than ship a silent no-op."
    );
    println!(
        "  NOTE: COMPOSED-PATH measurement -- the sequence is target-triggered, not causally tick-triggered; `OrderStore::place` (lib.rs:623) and the cross-publication block (lib.rs:571-604, NATS/Kafka -- off-path per ADR-004 §1) are excluded, matching the live loop's order-emit boundary."
    );
}

fn bench_tick_to_trade(c: &mut Criterion) {
    // UNTIMED, once: see `assert_scenario_nets`'s doc comment.
    assert_scenario_nets();

    hdr_report_batched(
        "tick_to_trade (composed T2T, ADR-004 §1)",
        setup_sample,
        run_t2t,
    );

    c.bench_function("tick_to_trade", |b| {
        // `iter_batched_ref`, NOT `iter_batched`: see `run_t2t`'s and
        // `hdr_report_batched`'s doc comments for why. Also see the module
        // doc's "Two reported series measure different shapes" section --
        // this series is a relative regression signal, not a second HDR
        // measurement of the same per-sample shape.
        b.iter_batched_ref(setup_sample, run_t2t, BatchSize::SmallInput);
    });
}

criterion_group!(benches, bench_tick_to_trade);
criterion_main!(benches);
