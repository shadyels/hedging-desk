//! Criterion regression benches + HDR-histogram p50/p99/p99.9 report for the
//! two hot-path operations d1-core exposes in M1 (docs/ROADMAP.md: "the
//! latency measurement exists before the features do"). Runs under the
//! `bench` profile, which inherits `release` (delta-one/Cargo.toml) — never
//! run this on a debug build.
#![allow(missing_docs)] // bench binary, not a public library API

use criterion::{Criterion, black_box, criterion_group, criterion_main};
use d1_core::{BookId, FeedTick, InstrumentId, MarketData, PositionKeeper, Side};

const N_INSTRUMENTS: u32 = 16;
const N_BOOKS: u32 = 4;
const HDR_SAMPLES: u32 = 100_000;

fn instrument_ids() -> Vec<InstrumentId> {
    (0..N_INSTRUMENTS).map(InstrumentId).collect()
}

fn book_ids() -> Vec<BookId> {
    (0..N_BOOKS).map(BookId).collect()
}

// Bench code measures the hot path but isn't on it — the expect_used carve-out
// delta-one/CLAUDE.md grants any clearly marked non-hot module.
#[allow(clippy::expect_used)]
fn hdr_report(name: &str, mut op: impl FnMut()) {
    let mut hist = hdrhistogram::Histogram::<u64>::new(3).expect("valid histogram sigfigs");
    for _ in 0..HDR_SAMPLES {
        let start = std::time::Instant::now();
        op();
        let elapsed_ns = start.elapsed().as_nanos() as u64;
        let _ = hist.record(elapsed_ns);
    }
    println!(
        "{name}: p50={}ns p99={}ns p99.9={}ns",
        hist.value_at_quantile(0.50),
        hist.value_at_quantile(0.99),
        hist.value_at_quantile(0.999),
    );
}

fn bench_market_data_ingest(c: &mut Criterion) {
    let ids = instrument_ids();
    let mut market_data = MarketData::new(&ids);
    let tick = FeedTick {
        instrument_id: InstrumentId(0),
        bid_px_e9: 100_000_000_000,
        ask_px_e9: 100_010_000_000,
        last_px_e9: 100_005_000_000,
        exch_ts_ns: 1,
    };

    hdr_report("MarketData::ingest", || {
        black_box(market_data.ingest(black_box(&tick)));
    });

    c.bench_function("MarketData::ingest", |b| {
        b.iter(|| black_box(market_data.ingest(black_box(&tick))));
    });
}

fn bench_position_keeper_apply_fill(c: &mut Criterion) {
    let ids = instrument_ids();
    let books = book_ids();
    let mut keeper = PositionKeeper::new(&books, &ids);

    // Alternate buy/sell so net qty oscillates instead of growing unbounded
    // over hundreds of thousands of iterations.
    let mut buy_next = true;
    hdr_report("PositionKeeper::apply_fill", || {
        let side = if buy_next { Side::Buy } else { Side::Sell };
        buy_next = !buy_next;
        black_box(keeper.apply_fill(
            black_box(BookId(0)),
            black_box(InstrumentId(0)),
            black_box(side),
            black_box(100),
            black_box(100_000_000_000),
        ));
    });

    c.bench_function("PositionKeeper::apply_fill", |b| {
        let mut buy_next = true;
        b.iter(|| {
            let side = if buy_next { Side::Buy } else { Side::Sell };
            buy_next = !buy_next;
            black_box(keeper.apply_fill(
                black_box(BookId(0)),
                black_box(InstrumentId(0)),
                black_box(side),
                black_box(100),
                black_box(100_000_000_000),
            ))
        });
    });
}

criterion_group!(
    benches,
    bench_market_data_ingest,
    bench_position_keeper_apply_fill
);
criterion_main!(benches);
