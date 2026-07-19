//! Netting-cycle orchestration (P1.M3 Slice 2, docs/ROADMAP.md). Wires
//! `d1_netting::net` into the live core loop: tracks per-(book,instrument)
//! EXO targets and open parent orders, computes `demand_b = target_b -
//! position_b - inflight_b` (ADR-005 §1) per instrument, books internal
//! cross legs immediately, and mints one firm-wide parent order for the
//! residual. `HashMap`-based state is allowed here -- this module lives in
//! `crates/d1`, off the benchmarked hot path (`d1-core`/`d1-netting` stay
//! alloc-free), matching `run_core`'s existing `println!`-driven, non-hot
//! style.
//!
//! Scope (Slice 2): crosses are booked into the `PositionKeeper` here but
//! NOT published -- `InternalCrossNotice` on `d1.crosses` and `cross_id`
//! minting are Slice 3; Kafka crosses/allocations are M4.

use std::collections::HashMap;

use d1_core::{
    BookId, ClOrdId, CrossRecord, InstrumentId, Order, OrderStatus, PositionKeeper, Side, Target,
    TransferRequest,
};
use d1_netting::{BookDemand, Cross, MAX_BOOKS, NettingError, RefPxPolicy, net};

/// One book's currently-known EXO target for one instrument.
#[derive(Debug, Clone, Copy)]
struct TargetCell {
    target_qty_e2: i64,
    band_e2: i64,
}

/// A firm-level external order spanning books (`Order.book == BookId(0)`,
/// the reserved firm-level parent-order pre-allocation, `live.proto`
/// `ExecutionReport.book_id` doc comment). Real per-book attribution lives in
/// `weights`, never in `Order.book`.
#[derive(Debug, Clone)]
pub struct ParentOrder {
    /// Netting cycle that minted this parent order (internal lineage only,
    /// no wire field).
    pub cycle_id: u64,
    /// Instrument this parent order trades.
    pub instrument: InstrumentId,
    /// Buy or sell.
    pub side: Side,
    /// Total requested quantity, fixed-point x10^2. Equals `Σ weights`.
    pub order_qty_e2: i64,
    /// Cumulative quantity allocated back to books so far, fixed-point x10^2.
    pub cum_filled_e2: i64,
    /// Per-book residual share `m_b` this parent order was minted to cover,
    /// book-id ascending. `Σ weights == order_qty_e2`.
    pub weights: Vec<(BookId, i64)>,
    /// Cumulative quantity allocated to each book so far, parallel to
    /// `weights` (same index -> same book).
    pub allocated: Vec<i64>,
}

/// Output of one netting cycle (`NettingSession::on_target`).
#[derive(Debug, Clone)]
pub struct CycleOutput {
    /// Internal crosses this cycle produced. Already booked into the
    /// `PositionKeeper` by the time this is returned -- present here so the
    /// caller can publish `InternalCrossNotice` (`crates/d1/src/lib.rs`) and
    /// audit them.
    pub crosses_to_book: Vec<CrossRecord>,
    /// The firm-level parent order to place, if this cycle's residual demand
    /// wasn't fully absorbed by crosses or suppressed by the no-trade band.
    pub parent_order: Option<Order>,
}

/// Books one two-leg internal cross into `keeper` atomically via
/// `PositionKeeper::apply_cross` (both legs commit or neither does -- a
/// half-booked cross would leave a net-imbalanced firm position published as
/// a clean `InternalCrossNotice`, root CLAUDE.md #2) and, on success, mints a
/// fresh `cross_id` (UUIDv7). On a `None` booking failure (overflow in
/// either leg), logs one rejection line and returns `None` -- the caller
/// must not publish anything for it. The single booking path ADR-009
/// requires both `on_target` (netting-derived crosses) and `on_transfer`
/// (directed transfers) to share.
fn book_cross(
    keeper: &mut PositionKeeper,
    instrument: InstrumentId,
    buy_book: BookId,
    sell_book: BookId,
    qty_e2: i64,
    px_e9: i64,
    policy: RefPxPolicy,
) -> Option<CrossRecord> {
    if keeper
        .apply_cross(instrument, buy_book, sell_book, qty_e2, px_e9)
        .is_none()
    {
        eprintln!(
            "d1: cross not booked (overflow) buy_book={buy_book:?} sell_book={sell_book:?} instrument={instrument:?} qty_e2={qty_e2} -- not published"
        );
        return None;
    }
    Some(CrossRecord {
        cross_id: uuid::Uuid::now_v7(),
        instrument,
        buy_book,
        sell_book,
        qty_e2,
        ref_px_e9: px_e9,
        policy_id: policy.as_str(),
    })
}

/// Per-instrument netting-cycle state: known EXO targets and open parent
/// orders. One instance is shared across the whole core-thread lifetime
/// (`crates/d1/src/lib.rs::run_core`).
pub struct NettingSession {
    targets: HashMap<(InstrumentId, BookId), TargetCell>,
    parents: HashMap<ClOrdId, ParentOrder>,
    cycle_seq: u64,
    next_cl_ord_seq: u64,
    policy: RefPxPolicy,
}

impl NettingSession {
    /// New session. `next_cl_ord_seq` is the first `ClOrdId` sequence number
    /// this session may mint -- the caller owns sequencing for whatever it
    /// places before wiring this in (e.g. `run_core`'s CLI startup order).
    #[must_use]
    pub fn new(policy: RefPxPolicy, next_cl_ord_seq: u64) -> Self {
        Self {
            targets: HashMap::new(),
            parents: HashMap::new(),
            cycle_seq: 0,
            next_cl_ord_seq,
            policy,
        }
    }

    /// A currently-tracked parent order, if any, for read-only inspection.
    #[must_use]
    pub fn parent(&self, cl_ord_id: ClOrdId) -> Option<&ParentOrder> {
        self.parents.get(&cl_ord_id)
    }

    /// A currently-tracked parent order, mutable -- callers use this plus
    /// `allocate_fill` to book a delivered fill back to the books that
    /// funded it.
    pub fn parent_mut(&mut self, cl_ord_id: ClOrdId) -> Option<&mut ParentOrder> {
        self.parents.get_mut(&cl_ord_id)
    }

    /// Record a new/restated EXO target and run one netting cycle for its
    /// instrument: recompute every known book's residual demand for that
    /// instrument (`target - position - inflight`), net it via
    /// `d1_netting::net`, book any resulting internal cross legs into
    /// `keeper` immediately (load-bearing for the firm-wide conservation
    /// invariant and for idempotent re-netting -- the next cycle reads
    /// post-cross positions and won't regenerate them), and mint a parent
    /// order for the residual, if any.
    ///
    /// `ref_px_e9` is the cross/order reference price for this cycle
    /// (arrival mid from the synthetic feed, ADR-005 §4).
    pub fn on_target(
        &mut self,
        target: Target,
        keeper: &mut PositionKeeper,
        ref_px_e9: i64,
    ) -> Result<CycleOutput, NettingError> {
        // Validate before touching `self.targets`: `net()` below also
        // rejects a negative band, but only after every *previously stored*
        // cell for this instrument has already been folded into `demands`.
        // Inserting first would let one bad message (band_qty_e2 < 0 is
        // untrusted EXO wire input) permanently poison this instrument --
        // every future cycle, for any book, would keep re-including the
        // poisoned cell and keep failing, a persistent denial of netting for
        // that instrument rather than a single rejected message.
        if target.band_e2 < 0 {
            return Err(NettingError::InvalidBand { book: target.book });
        }

        self.targets.insert(
            (target.instrument, target.book),
            TargetCell {
                target_qty_e2: target.target_qty_e2,
                band_e2: target.band_e2,
            },
        );

        // Every book with a known target for this instrument participates
        // in this cycle -- book-id ascending, matching the engine's own
        // tie-break and this module's Hamilton tie-break.
        let mut demands: Vec<BookDemand> = Vec::new();
        for (&(instrument, book), cell) in &self.targets {
            if instrument != target.instrument {
                continue;
            }
            // Defensive only: every book that ever reached `on_target` was
            // already gated against the keeper's universe by the caller
            // (`run_core`), so this should always be `Some`.
            let Some(position) = keeper.position(book, instrument) else {
                continue;
            };
            let inflight = self.inflight(instrument, book)?;
            let demand_e2 = cell
                .target_qty_e2
                .checked_sub(position.net_qty_e2)
                .and_then(|d| d.checked_sub(inflight))
                .ok_or(NettingError::Overflow)?;
            demands.push(BookDemand {
                book,
                demand_e2,
                band_e2: cell.band_e2,
            });
        }
        demands.sort_unstable_by_key(|d| d.book);

        let mut cross_buf = [Cross {
            buy_book: BookId(0),
            sell_book: BookId(0),
            qty_e2: 0,
            px_e9: 0,
            policy: self.policy,
        }; MAX_BOOKS];
        let netted = net(&demands, ref_px_e9, self.policy, &mut cross_buf)?;
        let crosses = cross_buf.get(..netted.n_crosses).unwrap_or(&[]).to_vec();

        // Book cross legs immediately via the shared `book_cross` path: nets
        // to zero firm-wide and makes re-netting idempotent (root CLAUDE.md
        // #2: never leave a cross silently unbooked).
        let cross_records: Vec<CrossRecord> = crosses
            .iter()
            .filter_map(|c| {
                book_cross(
                    keeper,
                    target.instrument,
                    c.buy_book,
                    c.sell_book,
                    c.qty_e2,
                    c.px_e9,
                    c.policy,
                )
            })
            .collect();

        self.cycle_seq = self.cycle_seq.wrapping_add(1);
        let cycle_id = self.cycle_seq;

        // Residual weights: the engine returns crosses + net_external, not
        // per-book residuals, so derive residual_b = demand_b - signed cross
        // legs (ADR-005's reconciliation_per_book invariant: Σresidual ==
        // net_external).
        let mut weights: Vec<(BookId, i64)> = Vec::new();
        for d in &demands {
            let mut signed = 0i64;
            for c in &crosses {
                if c.buy_book == d.book {
                    signed = signed.checked_add(c.qty_e2).ok_or(NettingError::Overflow)?;
                }
                if c.sell_book == d.book {
                    signed = signed.checked_sub(c.qty_e2).ok_or(NettingError::Overflow)?;
                }
            }
            let residual = d
                .demand_e2
                .checked_sub(signed)
                .ok_or(NettingError::Overflow)?;
            let m_b = residual.checked_abs().ok_or(NettingError::Overflow)?;
            if m_b > 0 {
                weights.push((d.book, m_b));
            }
        }

        let parent_order = if netted.net_external_e2 != 0 {
            let side = if netted.net_external_e2 > 0 {
                Side::Buy
            } else {
                Side::Sell
            };
            let order_qty_e2 = netted
                .net_external_e2
                .checked_abs()
                .ok_or(NettingError::Overflow)?;
            let cl_ord_id = ClOrdId::from_seq(self.next_cl_ord_seq);
            self.next_cl_ord_seq += 1;

            let order = Order {
                cl_ord_id,
                book: BookId(0),
                instrument: target.instrument,
                side,
                order_qty_e2,
                limit_px_e9: 0,
                status: OrderStatus::New,
                cum_qty_e2: 0,
                leaves_qty_e2: 0,
                last_px_e9: 0,
            };

            let n_books = weights.len();
            self.parents.insert(
                cl_ord_id,
                ParentOrder {
                    cycle_id,
                    instrument: target.instrument,
                    side,
                    order_qty_e2,
                    cum_filled_e2: 0,
                    weights,
                    allocated: vec![0; n_books],
                },
            );

            println!(
                "d1: netting cycle_id={cycle_id} parent order cl_ord_id={cl_ord_id:?} instrument={:?} side={side:?} qty_e2={order_qty_e2} books={n_books}",
                target.instrument
            );

            Some(order)
        } else {
            None
        };

        Ok(CycleOutput {
            crosses_to_book: cross_records,
            parent_order,
        })
    }

    /// Book a directed internal transfer (ADR-009) as one immediate cross
    /// through the same `book_cross` path netting-derived crosses use: `req`
    /// buys into `req.to_book`, sells from `req.from_book`. No external
    /// order, no parent-order tracking -- positions move now, so the next
    /// `on_target` cycle for this instrument reads post-transfer positions
    /// and never re-nets this quantity. The caller (`crates/d1/src/lib.rs::run_core`)
    /// owns validation (from/to distinct, `qty_e2 > 0`, both books in the
    /// keeper's universe) before calling this.
    pub fn on_transfer(
        &mut self,
        req: TransferRequest,
        keeper: &mut PositionKeeper,
        ref_px_e9: i64,
    ) -> Option<CrossRecord> {
        book_cross(
            keeper,
            req.instrument,
            req.to_book,
            req.from_book,
            req.qty_e2,
            ref_px_e9,
            self.policy,
        )
    }

    /// `Σ` over currently-open parent orders for `instrument` of `book`'s
    /// unfilled pro-rata share, signed by each parent's side (`weights`
    /// stores magnitudes `m_b`, not signed quantities -- a Sell parent's
    /// unfilled share reduces demand by a *negative* amount, exactly
    /// undoing that order's Sell once it fills, not a positive one).
    /// Fully-filled parents contribute zero and are left in the map rather
    /// than reclaimed -- same append-only-slab tradeoff `d1_core::OrderStore`
    /// already makes for a single demo session.
    ///
    /// `checked_*` throughout, matching `on_target`'s own overflow
    /// discipline: a saturated inflight would silently distort demand
    /// instead of surfacing `NettingError::Overflow` at the trust boundary.
    fn inflight(&self, instrument: InstrumentId, book: BookId) -> Result<i64, NettingError> {
        let mut total = 0i64;
        for parent in self.parents.values() {
            if parent.instrument != instrument {
                continue;
            }
            let sign: i64 = match parent.side {
                Side::Buy => 1,
                Side::Sell => -1,
            };
            for (i, &(b, m)) in parent.weights.iter().enumerate() {
                if b == book {
                    let allocated = parent.allocated.get(i).copied().unwrap_or(0);
                    let unfilled = m.checked_sub(allocated).ok_or(NettingError::Overflow)?;
                    let signed = sign.checked_mul(unfilled).ok_or(NettingError::Overflow)?;
                    total = total.checked_add(signed).ok_or(NettingError::Overflow)?;
                }
            }
        }
        Ok(total)
    }
}

/// Allocate a delivered fill against a parent order's residual weights,
/// pro-rata, by apportioning the *incremental* fill directly across each
/// book's remaining capacity (`m_b - allocated_b`) via `hamilton`, not by
/// diffing two independent cumulative apportionments.
///
/// This matters: largest-remainder apportionment suffers the Alabama
/// paradox -- a book's cumulative share can *decrease* as the cumulative
/// total grows (e.g. weights `[49,27,3]`, `hamilton(cumulative=13)` and
/// `hamilton(cumulative=14)` can disagree on book 3's share by -1). Diffing
/// two such calls would hand `PositionKeeper::apply_fill` a negative
/// quantity for a one-sided (Buy-only or Sell-only) parent order -- a
/// corrupted allocation and, come M4, an invalid `posttrade.allocations`
/// audit record. Apportioning against *remaining* capacity each call is
/// monotone by construction: every `hamilton` output is `>= 0` and
/// `<= remaining_b`, so `Σ delta == fill_qty_e2` exactly, every delta is
/// `>= 0`, and at full fill (`fill == Σ remaining`) each book lands exactly
/// on its `m_b` (a division by its own total is exact, no rounding).
/// Returns only the books that received a nonzero delta this call, i.e.
/// what to hand `PositionKeeper::apply_fill` for this fill.
#[must_use]
pub fn allocate_fill(parent: &mut ParentOrder, fill_qty_e2: i64) -> Vec<(BookId, i64)> {
    let remaining: Vec<(BookId, i64)> = parent
        .weights
        .iter()
        .enumerate()
        .map(|(i, &(book, m))| {
            let allocated = parent.allocated.get(i).copied().unwrap_or(0);
            (book, m.saturating_sub(allocated))
        })
        .collect();
    let remaining_total: i64 = remaining.iter().map(|(_, r)| *r).sum();
    let fill_qty_e2 = fill_qty_e2.clamp(0, remaining_total);

    let deltas = hamilton(&remaining, fill_qty_e2);

    let mut out = Vec::with_capacity(parent.weights.len());
    for (i, &delta) in deltas.iter().enumerate() {
        if delta == 0 {
            continue;
        }
        if let Some(&(book, _)) = parent.weights.get(i) {
            out.push((book, delta));
        }
        if let Some(slot) = parent.allocated.get_mut(i) {
            *slot = slot.saturating_add(delta);
        }
    }
    parent.cum_filled_e2 = parent.cum_filled_e2.saturating_add(fill_qty_e2);
    out
}

/// Largest-remainder (Hamilton) apportionment: split `total` across
/// `weights` proportional to each `m_b`, `Σ result == total` exactly.
/// Deterministic tie-break: largest fractional remainder first, then
/// ascending `book_id`. `i128` intermediates on the multiply/divide keep
/// this infallible for any `i64` inputs.
fn hamilton(weights: &[(BookId, i64)], total: i64) -> Vec<i64> {
    let n = weights.len();
    let sum_weights: i64 = weights.iter().map(|(_, w)| *w).sum();
    if sum_weights <= 0 || total <= 0 {
        return vec![0; n];
    }
    let total = total.clamp(0, sum_weights);
    let denom = i128::from(sum_weights);

    let mut floors = vec![0i64; n];
    let mut remainders = vec![0i128; n];
    for (i, &(_, w)) in weights.iter().enumerate() {
        let numerator = i128::from(w) * i128::from(total);
        let floor_i128 = numerator / denom;
        let remainder = numerator - floor_i128 * denom;
        if let Some(slot) = floors.get_mut(i) {
            *slot = i64::try_from(floor_i128).unwrap_or(i64::MAX);
        }
        if let Some(slot) = remainders.get_mut(i) {
            *slot = remainder;
        }
    }

    let sum_floor: i64 = floors.iter().sum();
    let mut deficit = total.saturating_sub(sum_floor);

    let mut order: Vec<usize> = (0..n).collect();
    order.sort_unstable_by(|&a, &b| {
        let ra = remainders.get(a).copied().unwrap_or(0);
        let rb = remainders.get(b).copied().unwrap_or(0);
        rb.cmp(&ra).then_with(|| {
            let ba = weights.get(a).map_or(BookId(0), |(book, _)| *book);
            let bb = weights.get(b).map_or(BookId(0), |(book, _)| *book);
            ba.cmp(&bb)
        })
    });

    for i in order {
        if deficit <= 0 {
            break;
        }
        if let Some(slot) = floors.get_mut(i) {
            *slot = slot.saturating_add(1);
        }
        deficit -= 1;
    }

    floors
}

#[cfg(test)]
#[allow(clippy::unwrap_used)] // tests: unwrap_used/expect_used are hot-path-only bans (delta-one/CLAUDE.md)
mod tests {
    use super::*;
    use proptest::prelude::*;
    use std::collections::HashMap as StdHashMap;

    fn target(book: u32, instrument: u32, target_qty_e2: i64, band_e2: i64) -> Target {
        Target {
            book: BookId(book),
            instrument: InstrumentId(instrument),
            target_qty_e2,
            band_e2,
        }
    }

    #[test]
    fn allocation_totality_and_exact_at_full_fill() {
        let mut parent = ParentOrder {
            cycle_id: 1,
            instrument: InstrumentId(1001),
            side: Side::Buy,
            order_qty_e2: 100,
            cum_filled_e2: 0,
            weights: vec![(BookId(1), 30), (BookId(2), 30), (BookId(3), 40)],
            allocated: vec![0, 0, 0],
        };
        let mut totals: StdHashMap<BookId, i64> = StdHashMap::new();
        for chunk in [17, 33, 25, 25] {
            let alloc = allocate_fill(&mut parent, chunk);
            let sum: i64 = alloc.iter().map(|(_, q)| *q).sum();
            assert_eq!(
                sum, chunk,
                "each call's allocation must sum to the fill it covers"
            );
            for (book, qty) in alloc {
                *totals.entry(book).or_insert(0) += qty;
            }
        }
        assert_eq!(totals.get(&BookId(1)).copied().unwrap_or(0), 30);
        assert_eq!(totals.get(&BookId(2)).copied().unwrap_or(0), 30);
        assert_eq!(totals.get(&BookId(3)).copied().unwrap_or(0), 40);
        assert_eq!(parent.cum_filled_e2, 100);
    }

    #[test]
    fn allocation_is_monotone_alabama_paradox_regression() {
        // Reviewer repro: weights [49,27,3] -- diffing two independent
        // `hamilton(cumulative)` calls at cumulative 13 then 14 used to
        // emit a -1 delta for book 3 (the Alabama paradox: its cumulative
        // share can shrink as the cumulative total grows). Apportioning
        // each incremental fill against remaining capacity instead must
        // never produce a negative delta, for any call.
        let mut parent = ParentOrder {
            cycle_id: 1,
            instrument: InstrumentId(1001),
            side: Side::Buy,
            order_qty_e2: 79,
            cum_filled_e2: 0,
            weights: vec![(BookId(1), 49), (BookId(2), 27), (BookId(3), 3)],
            allocated: vec![0, 0, 0],
        };

        let mut totals: StdHashMap<BookId, i64> = StdHashMap::new();
        for chunk in [13, 1, 65] {
            // 13, then 14, then fully filled at 79.
            let alloc = allocate_fill(&mut parent, chunk);
            let sum: i64 = alloc.iter().map(|(_, q)| *q).sum();
            assert_eq!(
                sum, chunk,
                "every call's allocation must sum to the fill it covers"
            );
            for &(book, qty) in &alloc {
                assert!(
                    qty >= 0,
                    "allocation delta must never be negative (Alabama paradox regression)"
                );
                *totals.entry(book).or_insert(0) += qty;
            }
        }

        assert_eq!(totals.get(&BookId(1)).copied().unwrap_or(0), 49);
        assert_eq!(totals.get(&BookId(2)).copied().unwrap_or(0), 27);
        assert_eq!(totals.get(&BookId(3)).copied().unwrap_or(0), 3);
        assert_eq!(parent.cum_filled_e2, 79);
    }

    #[test]
    fn cross_leg_sum_zero_when_two_books_cross() {
        // Book 1's demand is suppressed by its own band (no external order,
        // no inflight commit -- its demand stays fresh), so when book 2's
        // opposite demand arrives, both are present in the same cycle and
        // cross.
        let mut keeper = PositionKeeper::new(&[BookId(1), BookId(2)], &[InstrumentId(1001)]);
        let mut session = NettingSession::new(RefPxPolicy::ArrivalMid, 100);

        let out1 = session
            .on_target(target(1, 1001, 500, 1_000), &mut keeper, 1)
            .unwrap();
        assert!(out1.crosses_to_book.is_empty());
        assert!(out1.parent_order.is_none(), "suppressed by band, no order");

        let out2 = session
            .on_target(target(2, 1001, -500, 0), &mut keeper, 1)
            .unwrap();
        assert_eq!(out2.crosses_to_book.len(), 1);
        assert!(out2.parent_order.is_none(), "exact offset, no residual");

        // Cross legs net to zero firm-wide.
        let p1 = keeper.position(BookId(1), InstrumentId(1001)).unwrap();
        let p2 = keeper.position(BookId(2), InstrumentId(1001)).unwrap();
        assert_eq!(p1.net_qty_e2 + p2.net_qty_e2, 0);
        assert_eq!(p1.net_qty_e2, 500);
        assert_eq!(p2.net_qty_e2, -500);
    }

    #[test]
    fn demand_includes_inflight_unchanged_restatement_places_no_order() {
        let mut keeper = PositionKeeper::new(&[BookId(1)], &[InstrumentId(1001)]);
        let mut session = NettingSession::new(RefPxPolicy::ArrivalMid, 1);

        let out1 = session
            .on_target(target(1, 1001, 500, 0), &mut keeper, 1)
            .unwrap();
        let order1 = out1.parent_order.unwrap();
        assert_eq!(order1.order_qty_e2, 500);

        // Restate the identical target before the first order's fill
        // returns: demand must be fully absorbed by inflight now.
        let out2 = session
            .on_target(target(1, 1001, 500, 0), &mut keeper, 1)
            .unwrap();
        assert!(
            out2.parent_order.is_none(),
            "restating an unchanged target while the original order is still in flight must not over-order"
        );
    }

    #[test]
    fn negative_band_errors_not_panics() {
        let mut keeper = PositionKeeper::new(&[BookId(1)], &[InstrumentId(1001)]);
        let mut session = NettingSession::new(RefPxPolicy::ArrivalMid, 1);
        let err = session
            .on_target(target(1, 1001, 500, -1), &mut keeper, 1)
            .unwrap_err();
        assert_eq!(err, NettingError::InvalidBand { book: BookId(1) });
    }

    #[test]
    fn negative_band_does_not_poison_later_cycles_for_the_instrument() {
        // A single bad TargetPosition (band_qty_e2 < 0 is untrusted EXO wire
        // input) must be rejected as a one-off, not remembered: a later
        // valid target for a *different* book on the same instrument must
        // still net cleanly. Regression for the bug where the invalid cell
        // was inserted into session state before validation, so every
        // subsequent cycle for the instrument kept re-including it and kept
        // failing.
        let mut keeper = PositionKeeper::new(&[BookId(1), BookId(2)], &[InstrumentId(1001)]);
        let mut session = NettingSession::new(RefPxPolicy::ArrivalMid, 1);

        let err = session
            .on_target(target(1, 1001, 500, -1), &mut keeper, 1)
            .unwrap_err();
        assert_eq!(err, NettingError::InvalidBand { book: BookId(1) });

        let out = session
            .on_target(target(2, 1001, 300, 0), &mut keeper, 1)
            .unwrap();
        assert!(
            out.parent_order.is_some(),
            "instrument must still net after an earlier rejected message, not stay poisoned"
        );
    }

    #[test]
    fn on_transfer_books_both_legs_and_stamps_lineage() {
        let mut keeper = PositionKeeper::new(&[BookId(1), BookId(5)], &[InstrumentId(1001)]);
        let mut session = NettingSession::new(RefPxPolicy::ArrivalMid, 1);

        let record = session
            .on_transfer(
                TransferRequest {
                    instrument: InstrumentId(1001),
                    from_book: BookId(1),
                    to_book: BookId(5),
                    qty_e2: 40_000,
                },
                &mut keeper,
                150_000_000_000,
            )
            .unwrap();

        assert_eq!(record.buy_book, BookId(5));
        assert_eq!(record.sell_book, BookId(1));
        assert_eq!(record.qty_e2, 40_000);
        assert_eq!(record.ref_px_e9, 150_000_000_000);
        assert_eq!(record.policy_id, "ARRIVAL_MID");
        assert_eq!(
            keeper
                .position(BookId(5), InstrumentId(1001))
                .unwrap()
                .net_qty_e2,
            40_000
        );
        assert_eq!(
            keeper
                .position(BookId(1), InstrumentId(1001))
                .unwrap()
                .net_qty_e2,
            -40_000
        );
    }

    #[test]
    fn on_transfer_overflow_rejects_and_moves_no_position() {
        // Security-review regression: a transfer that can't book atomically
        // must return `None` and leave both books' positions untouched, not
        // half-book one leg.
        let mut keeper = PositionKeeper::new(&[BookId(1), BookId(5)], &[InstrumentId(1001)]);
        let mut session = NettingSession::new(RefPxPolicy::ArrivalMid, 1);

        // Force the buy leg (to_book) to overflow.
        keeper
            .apply_fill(BookId(5), InstrumentId(1001), Side::Buy, i64::MAX, 1)
            .unwrap();

        let record = session.on_transfer(
            TransferRequest {
                instrument: InstrumentId(1001),
                from_book: BookId(1),
                to_book: BookId(5),
                qty_e2: 1,
            },
            &mut keeper,
            1,
        );

        assert!(record.is_none());
        assert_eq!(
            keeper
                .position(BookId(1), InstrumentId(1001))
                .unwrap()
                .net_qty_e2,
            0
        );
        assert_eq!(
            keeper
                .position(BookId(5), InstrumentId(1001))
                .unwrap()
                .net_qty_e2,
            i64::MAX
        );
    }

    proptest! {
        #[test]
        fn transfer_conserves_firm_position(
            qty_e2 in 1i64..=1_000_000,
            px_e9 in 1i64..=1_000_000_000_000,
        ) {
            // Money path (transfers move real risk, ADR-009): a directed
            // transfer must never change the firm-wide sum of book
            // positions, only redistribute it -- `to_book` gains exactly
            // `qty_e2`, `from_book` loses exactly `qty_e2`.
            let mut keeper = PositionKeeper::new(&[BookId(1), BookId(5)], &[InstrumentId(1001)]);
            let mut session = NettingSession::new(RefPxPolicy::ArrivalMid, 1);

            let before_sum = keeper.position(BookId(1), InstrumentId(1001)).unwrap().net_qty_e2
                + keeper.position(BookId(5), InstrumentId(1001)).unwrap().net_qty_e2;

            let _record = session.on_transfer(
                TransferRequest {
                    instrument: InstrumentId(1001),
                    from_book: BookId(1),
                    to_book: BookId(5),
                    qty_e2,
                },
                &mut keeper,
                px_e9,
            );

            let after_from = keeper.position(BookId(1), InstrumentId(1001)).unwrap().net_qty_e2;
            let after_to = keeper.position(BookId(5), InstrumentId(1001)).unwrap().net_qty_e2;

            prop_assert_eq!(after_to, qty_e2);
            prop_assert_eq!(after_from, -qty_e2);
            prop_assert_eq!(after_from + after_to, before_sum);
        }
    }
}
