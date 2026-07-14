//! Criterion regression benches + HDR-histogram p50/p99/p99.9 report for the
//! two hot-path operations d1-core exposes in M1 (docs/ROADMAP.md: "the
//! latency measurement exists before the features do"). Runs under the
//! `bench` profile, which inherits `release` (delta-one/Cargo.toml) — never
//! run this on a debug build.
#![allow(missing_docs)] // bench binary, not a public library API

use criterion::{Criterion, black_box, criterion_group, criterion_main};
use d1_core::{
    BookId, ClOrdId, ExecEvent, ExecId, FeedTick, InstrumentId, MarketData, Order, OrderStatus,
    OrderStore, PositionKeeper, Side,
};

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

// Unique per-iteration exec id, no allocation (mirrors `ClOrdId::from_seq`).
fn seq_exec_id(mut n: u64) -> ExecId {
    let mut bytes = [b'0'; 20];
    for slot in bytes.iter_mut().rev() {
        *slot = b'0' + (n % 10) as u8;
        n /= 10;
    }
    ExecId::from_bytes(bytes)
}

fn bench_order_store_apply_exec(c: &mut Criterion) {
    let mut store = OrderStore::new(1);
    let cl_ord_id = ClOrdId::from_seq(0);
    // Huge order_qty so partial fills never reach the terminal Filled state
    // over hundreds of thousands of iterations.
    store.place(Order {
        cl_ord_id,
        book: BookId(0),
        instrument: InstrumentId(0),
        side: Side::Buy,
        order_qty_e2: i64::MAX / 2,
        limit_px_e9: 0,
        status: OrderStatus::New,
        cum_qty_e2: 0,
        leaves_qty_e2: 0,
        last_px_e9: 0,
    });

    let mut seq: u64 = 0;
    hdr_report("OrderStore::apply_exec", || {
        seq += 1;
        let _ = black_box(store.apply_exec(black_box(&ExecEvent {
            cl_ord_id,
            exec_id: seq_exec_id(seq),
            reported_status: OrderStatus::PartiallyFilled,
            last_qty_e2: 100,
            last_px_e9: 100_000_000_000,
        })));
    });

    c.bench_function("OrderStore::apply_exec", |b| {
        b.iter(|| {
            seq += 1;
            black_box(store.apply_exec(black_box(&ExecEvent {
                cl_ord_id,
                exec_id: seq_exec_id(seq),
                reported_status: OrderStatus::PartiallyFilled,
                last_qty_e2: 100,
                last_px_e9: 100_000_000_000,
            })))
        });
    });
}

criterion_group!(
    benches,
    bench_market_data_ingest,
    bench_position_keeper_apply_fill,
    bench_order_store_apply_exec
);
criterion_main!(benches);
