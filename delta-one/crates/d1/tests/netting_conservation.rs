//! Mandatory P1.M3 Slice 2 proof: firm-wide position conservation holds
//! under arbitrary fill interleaving, not just at rest. `docs/ROADMAP.md`.
//!
//! Drives `d1::cycle::NettingSession` + `d1_core::PositionKeeper` directly
//! (no rings/threads/FIX -- this is a pure state-machine property, not a
//! wire-level integration test like `nats_round_trip.rs`). Starting from
//! flat positions and one EXO target per book (no-trade band = 0), the
//! invariant proven is: at every point during fill delivery, `Σ_book
//! net_qty_e2 == signed quantity filled so far` across all open parent
//! orders (internal cross legs always net to zero firm-wide, whether or not
//! any fire for a given case -- see `crates/d1/src/cycle.rs`'s own
//! `cross_leg_sum_zero_when_two_books_cross` unit test for a case where
//! they do); and once every parent order is fully filled, each targeted
//! book's position lands exactly on its target (Hamilton apportionment's
//! "no drift at full fill" guarantee).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)] // proptest, not hot-path code

use d1::cycle::{NettingSession, allocate_fill};
use d1_core::{BookId, InstrumentId, PositionKeeper, Side, Target};
use d1_netting::RefPxPolicy;
use proptest::prelude::*;

const INSTRUMENT: InstrumentId = InstrumentId(1001);
const ALL_BOOKS: [u32; 3] = [1, 2, 3];

/// Deterministic LCG step -- same constant as `d1-netting`'s
/// `determinism_under_permutation` test (`crates/d1-netting/src/lib.rs`),
/// reused here rather than pulling in an RNG crate.
fn lcg_next(state: &mut u64) -> u64 {
    *state = state.wrapping_mul(6364136223846793005).wrapping_add(1);
    *state >> 33
}

/// Split `total` (> 0) into an ordered sequence of positive chunks summing
/// exactly to `total`, driven by `seed`.
fn partition(total: i64, seed: u64) -> Vec<i64> {
    let mut state = seed | 1;
    let mut remaining = total;
    let mut chunks = Vec::new();
    while remaining > 0 {
        let r = lcg_next(&mut state);
        #[allow(clippy::cast_sign_loss)] // remaining > 0 here, fits u64 exactly
        let take = 1 + (r % remaining as u64) as i64;
        chunks.push(take);
        remaining -= take;
    }
    chunks
}

/// Riffle-merge each parent's own ordered chunk sequence into one
/// arbitrarily-interleaved delivery order, preserving each parent's
/// internal chunk order (an `ExecEvent`'s `last_qty_e2` always applies to
/// the next unfilled slice of its own order, never out of sequence).
/// Returns `(stream_index, chunk)` pairs.
fn interleave(streams: &[Vec<i64>], seed: u64) -> Vec<(usize, i64)> {
    let mut state = seed | 1;
    let mut pointers = vec![0usize; streams.len()];
    let mut out = Vec::new();
    loop {
        let available: Vec<usize> = (0..streams.len())
            .filter(|&i| {
                let ptr = pointers.get(i).copied().unwrap_or(0);
                let len = streams.get(i).map_or(0, Vec::len);
                ptr < len
            })
            .collect();
        if available.is_empty() {
            break;
        }
        let idx = (lcg_next(&mut state) as usize) % available.len();
        let Some(&pick) = available.get(idx) else {
            break;
        };
        let ptr = pointers.get(pick).copied().unwrap_or(0);
        let Some(&chunk) = streams.get(pick).and_then(|s| s.get(ptr)) else {
            break;
        };
        out.push((pick, chunk));
        if let Some(slot) = pointers.get_mut(pick) {
            *slot += 1;
        }
    }
    out
}

/// 2 or 3 books drawn from `{1,2,3}`, paired with one target per book.
fn scenario_strategy() -> impl Strategy<Value = (Vec<u32>, Vec<i64>)> {
    prop::sample::subsequence(ALL_BOOKS.to_vec(), 2..=3).prop_flat_map(|books| {
        let n = books.len();
        (
            Just(books),
            prop::collection::vec(-1_000_000i64..=1_000_000, n),
        )
    })
}

proptest! {
    #[test]
    fn firm_wide_position_conserved_under_arbitrary_fill_interleaving(
        (books, targets) in scenario_strategy(),
        partition_seed in any::<u64>(),
        interleave_seed in any::<u64>(),
    ) {
        let book_ids: Vec<BookId> = books.iter().map(|&b| BookId(b)).collect();
        let mut keeper = PositionKeeper::new(&book_ids, &[INSTRUMENT]);
        let mut session = NettingSession::new(RefPxPolicy::ArrivalMid, 1);

        // Feed one target per book, ascending book id, no-trade band = 0.
        let mut parent_ids = Vec::new();
        for (&book, &target_qty_e2) in books.iter().zip(targets.iter()) {
            let target = Target {
                book: BookId(book),
                instrument: INSTRUMENT,
                target_qty_e2,
                band_e2: 0,
            };
            let output = session.on_target(target, &mut keeper, 1).unwrap();
            for c in &output.crosses_to_book {
                prop_assert!(c.qty_e2 > 0);
            }
            if let Some(order) = output.parent_order {
                parent_ids.push(order.cl_ord_id);
            }
        }

        // Chunk each parent's total into an ordered partition, then
        // riffle-merge all parents' chunk streams into one arbitrary
        // interleaved delivery order.
        let streams: Vec<Vec<i64>> = parent_ids
            .iter()
            .enumerate()
            .map(|(i, &cl_ord_id)| {
                let m = session.parent(cl_ord_id).unwrap().order_qty_e2;
                partition(m, partition_seed.wrapping_add(i as u64))
            })
            .collect();
        let delivery = interleave(&streams, interleave_seed);

        let mut signed_filled_total: i64 = 0;
        for (stream_idx, chunk) in delivery {
            let Some(&cl_ord_id) = parent_ids.get(stream_idx) else {
                continue;
            };
            let side = session.parent(cl_ord_id).unwrap().side;
            let parent = session.parent_mut(cl_ord_id).unwrap();
            let allocations = allocate_fill(parent, chunk);
            let alloc_sum: i64 = allocations.iter().map(|(_, q)| *q).sum();
            prop_assert_eq!(alloc_sum, chunk, "an allocation must always account for the whole fill it covers");

            for (book, qty_e2) in allocations {
                keeper.apply_fill(book, INSTRUMENT, side, qty_e2, 1).unwrap();
            }
            signed_filled_total += match side {
                Side::Buy => chunk,
                Side::Sell => -chunk,
            };

            let sum_positions: i64 = book_ids
                .iter()
                .map(|&b| keeper.position(b, INSTRUMENT).unwrap().net_qty_e2)
                .sum();
            // Internal cross legs (booked in `on_target`, before any fill
            // in this loop) always net to zero firm-wide, so the running
            // sum of book positions equals exactly what's been filled
            // externally so far.
            prop_assert_eq!(sum_positions, signed_filled_total);
        }

        // Terminal: every parent order is now fully filled (all its chunks
        // were delivered), so every targeted book's position lands exactly
        // on its target -- no drift from Hamilton apportionment, and no
        // firm position left stranded in an unbooked cross or partial fill.
        for (&book, &target_qty_e2) in books.iter().zip(targets.iter()) {
            let pos = keeper.position(BookId(book), INSTRUMENT).unwrap().net_qty_e2;
            prop_assert_eq!(pos, target_qty_e2);
        }
    }
}
