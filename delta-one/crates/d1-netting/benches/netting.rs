//! Criterion regression bench + HDR-histogram p50/p99/p99.9 report for
//! `d1_netting::net`, mirroring `crates/d1-core/benches/hot_path.rs`. Runs
//! under the `bench` profile, which inherits `release`
//! (delta-one/Cargo.toml) — never run this on a debug build.
#![allow(missing_docs)] // bench binary, not a public library API

use criterion::{Criterion, black_box, criterion_group, criterion_main};
use d1_core::BookId;
use d1_netting::{BookDemand, Cross, RefPxPolicy, net};

const HDR_SAMPLES: u32 = 100_000;
const REF_PX_E9: i64 = 150_000_000_000;

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

fn two_book_demands() -> [BookDemand; 2] {
    [
        BookDemand {
            book: BookId(1),
            demand_e2: 1_000_000,
            band_e2: 0,
        },
        BookDemand {
            book: BookId(2),
            demand_e2: -800_000,
            band_e2: 0,
        },
    ]
}

fn five_book_demands() -> [BookDemand; 5] {
    [
        BookDemand {
            book: BookId(1),
            demand_e2: 900_000,
            band_e2: 0,
        },
        BookDemand {
            book: BookId(2),
            demand_e2: 400_000,
            band_e2: 0,
        },
        BookDemand {
            book: BookId(3),
            demand_e2: -300_000,
            band_e2: 0,
        },
        BookDemand {
            book: BookId(4),
            demand_e2: -500_000,
            band_e2: 0,
        },
        BookDemand {
            book: BookId(5),
            demand_e2: -200_000,
            band_e2: 0,
        },
    ]
}

fn empty_cross_slot() -> Cross {
    Cross {
        buy_book: BookId(0),
        sell_book: BookId(0),
        qty_e2: 0,
        px_e9: 0,
        policy: RefPxPolicy::ArrivalMid,
    }
}

fn bench_net_two_books(c: &mut Criterion) {
    let demands = two_book_demands();
    let mut out = [empty_cross_slot(); 4];

    hdr_report("net (2 books)", || {
        let _ = black_box(net(
            black_box(&demands),
            black_box(REF_PX_E9),
            black_box(RefPxPolicy::ArrivalMid),
            black_box(&mut out),
        ));
    });

    c.bench_function("net (2 books)", |b| {
        b.iter(|| {
            black_box(net(
                black_box(&demands),
                black_box(REF_PX_E9),
                black_box(RefPxPolicy::ArrivalMid),
                black_box(&mut out),
            ))
        });
    });
}

fn bench_net_five_books(c: &mut Criterion) {
    let demands = five_book_demands();
    let mut out = [empty_cross_slot(); 8];

    hdr_report("net (5 books)", || {
        let _ = black_box(net(
            black_box(&demands),
            black_box(REF_PX_E9),
            black_box(RefPxPolicy::ArrivalMid),
            black_box(&mut out),
        ));
    });

    c.bench_function("net (5 books)", |b| {
        b.iter(|| {
            black_box(net(
                black_box(&demands),
                black_box(REF_PX_E9),
                black_box(RefPxPolicy::ArrivalMid),
                black_box(&mut out),
            ))
        });
    });
}

criterion_group!(benches, bench_net_two_books, bench_net_five_books);
criterion_main!(benches);
