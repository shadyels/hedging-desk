//! d1-analytics — tracker analytics (TE, tracking difference, cash drag). ADR-010. Off hot path.
//!
//! Pure calculator: the ONLY dependency is `d1-core`, for the `BookId`
//! newtype (never a bare `u32` across module boundaries, per
//! delta-one/CLAUDE.md's Rust guardrails). This crate holds the rolling
//! sampled WINDOW of per-period returns; it never holds positions or cash --
//! `crates/d1/src/lib.rs::run_core` reads `d1_core::PositionKeeper` /
//! `d1_core::MarketData` each sample and pushes the resulting `Sample` in.
//!
//! Integer-only math, no floats, no third-party bignum crate: returns are
//! `_e9` fixed point, so a squared return lands in `_e18`, and `u128::isqrt`
//! (stable std since Rust 1.84; this workspace pins 1.88) applied to an
//! `_e18` value yields an `_e9` standard deviation directly --
//! `sqrt(x * 10^18) == sqrt(x) * 10^9`. Width discipline is mandatory, not
//! defensive: every accumulator in this module is `i128`/`u128`, narrowing to
//! the wire-sized `i64`/`u32`/`u64` types ONLY at the very end via
//! `checked`/`try_from`, mirroring `d1-core::keeper`'s overflow posture --
//! `None` on overflow rather than a panic or a silent wraparound, however
//! extreme the sampled inputs are.

use std::collections::VecDeque;

use d1_core::BookId;

/// Annualization basis: `d1.toml [tracker]`'s `sampling_interval_s` comment
/// documents that one sample represents a demo-time "day", so a year is 252
/// trading days regardless of how short `sampling_interval_s` is configured
/// for a test. `sampling_interval_s` must NEVER enter this constant or any
/// formula below -- shortening it for a test must not change the metric.
pub const TRADING_DAYS_PER_YEAR: i64 = 252;

/// `_e9` fixed-point representation of one period's simple return:
/// `(now - prev) * 1e9 / prev`. `None` when `prev == 0` (no baseline to
/// return from) or the result does not fit back into `i64`.
///
/// THE single place the width discipline in this crate lives -- every return
/// in the crate is produced by this function. `prev`/`now` are themselves
/// `_e9` prices or NAVs, up to `i64::MAX` (~9.2e18) in an extreme case, so
/// `(now - prev) * 1e9` can reach ~9.2e27, far past `i64`: the subtraction,
/// scaling and division all happen in `i128` before the final checked
/// narrowing back to `i64`.
#[must_use]
pub fn period_return_e9(prev: i64, now: i64) -> Option<i64> {
    if prev == 0 {
        return None;
    }
    let delta = i128::from(now).checked_sub(i128::from(prev))?;
    let scaled = delta.checked_mul(1_000_000_000i128)?;
    let ratio = scaled.checked_div(i128::from(prev))?;
    i64::try_from(ratio).ok()
}

/// One sampled observation feeding a tracker book's rolling analytics window.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Sample {
    /// Synthetic exchange-clock timestamp this observation's sampling
    /// interval ends at (`d1_core::FeedTick::exch_ts_ns`'s clock --
    /// deterministic, never the wall clock).
    pub window_end_ns: u64,
    /// This book's NAV return over the period, `_e9`
    /// (`period_return_e9(prev_nav_e9, nav_e9)`).
    pub book_return_e9: i64,
    /// The book's benchmark's fixed-weight return over the same period,
    /// `_e9` (`Σ w_c · r_c`, `TrackerBook::constituents`' weights).
    pub bench_return_e9: i64,
    /// `cash_e9[book] / NAV_e9` at this sample, `_e9`.
    pub cash_weight_e9: i64,
}

/// Rolling window of `Sample`s for one tracker book, capped at
/// `te_window_obs` (ADR-010 §5, `d1.toml [tracker]`). Backed by a
/// `VecDeque` with its capacity allocated ONCE in `new` -- `push` evicts the
/// oldest sample past capacity rather than growing unbounded.
pub struct TrackerWindow {
    capacity: usize,
    samples: VecDeque<Sample>,
}

impl TrackerWindow {
    /// Preallocate a window holding at most `te_window_obs` samples.
    #[must_use]
    pub fn new(te_window_obs: usize) -> Self {
        Self {
            capacity: te_window_obs,
            samples: VecDeque::with_capacity(te_window_obs),
        }
    }

    /// Push one observation, evicting the oldest sample once `capacity` is
    /// reached. A `capacity == 0` window is a degenerate config (no
    /// analytics can ever come from it) and is a no-op rather than growing
    /// past its stated ceiling.
    pub fn push(&mut self, sample: Sample) {
        if self.capacity == 0 {
            return;
        }
        if self.samples.len() >= self.capacity {
            self.samples.pop_front();
        }
        self.samples.push_back(sample);
    }

    /// Current observation count (`<= capacity`).
    #[must_use]
    pub fn len(&self) -> usize {
        self.samples.len()
    }

    /// True when the window holds no observations yet.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.samples.is_empty()
    }
}

/// Output record for one tracker book's analytics sample -- the pure-Rust
/// counterpart of `TrackerAnalytics` (Protobuf), minus the `Meta`/wire
/// wrapping which is `d1-gateway-nats::convert`'s job. `Copy` and id-based
/// (`BookId`, never a `String`) because it crosses an `rtrb` SPSC ring
/// (ADR-013) from the core thread to the NATS gateway thread.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TrackerRecord {
    /// The tracker book this record is for.
    pub book: BookId,
    /// Annualized standard deviation of active (book-minus-benchmark)
    /// returns over the window, `_e9`.
    pub tracking_error_ann_e9: i64,
    /// Cumulative (arithmetic, NOT compounded) active return over the
    /// window, `_e9`.
    pub tracking_diff_e9: i64,
    /// Average cash weight over the window, `_e9`.
    pub cash_weight_e9: i64,
    /// Average cash weight x (cumulative benchmark return − cumulative cash
    /// return) over the window, `_e9`.
    pub cash_drag_e9: i64,
    /// Observation count backing this record (`>= 2`).
    pub n_obs: u32,
    /// Timestamp of the window's oldest retained sample.
    pub window_start_ns: u64,
    /// Timestamp of the window's newest (most recently pushed) sample.
    pub window_end_ns: u64,
}

/// `sqrt(periods_per_year)` as an `_e9` fixed-point constant, via the same
/// isqrt identity `period_return_e9`'s doc comment relies on:
/// `isqrt(periods * 1e18) == sqrt(periods) * 1e9`. `None` only if
/// `periods_per_year` is negative (never true for the one caller below,
/// `TRADING_DAYS_PER_YEAR`, but this stays total rather than assuming it).
fn sqrt_periods_e9(periods_per_year: i64) -> Option<u128> {
    let periods = u128::try_from(periods_per_year).ok()?;
    let scaled = periods.checked_mul(1_000_000_000_000_000_000u128)?;
    Some(scaled.isqrt())
}

/// Compute `book`'s analytics over `window`'s current contents. `None` below
/// two observations -- sample variance's `n − 1` denominator is undefined
/// (and meaningless) for `n < 2`, which is what makes that gate load-bearing
/// rather than an arbitrary minimum. `yield_annual_e9` is the demo cash-yield
/// convention (`d1_refdata::Universe::cash_yield_annual_e9`), needed for the
/// cash-drag term's `cash_ret_e9`; it is a caller parameter rather than a
/// `Sample` field because it is a firm-wide convention, not a per-observation
/// quantity.
///
/// Every accumulator here is `i128`/`u128`; a `None` return also covers
/// internal overflow (mirroring `d1-core::keeper`'s posture), so this never
/// panics regardless of how extreme the sampled returns are.
#[must_use]
pub fn analytics(
    book: BookId,
    window: &TrackerWindow,
    yield_annual_e9: i64,
) -> Option<TrackerRecord> {
    let n = window.samples.len();
    if n < 2 {
        return None;
    }
    let n_i128 = i128::try_from(n).ok()?;

    let first = window.samples.front()?;
    let last = window.samples.back()?;

    // Pass 1: sums needed for the means (active return, cash weight, bench
    // return). `active = book_return_e9 - bench_return_e9`, each an `i64` up
    // to ~9.223e18 in magnitude, so a single `active` can reach ~1.845e19
    // (one at +i64::MAX, the other near i64::MIN); Sigma over up to 250
    // observations then reaches ~4.6e21 -- past `i64::MAX` by many orders of
    // magnitude, which is exactly why every accumulator here is `i128`
    // throughout (this module's width-discipline doc comment) rather than a
    // defensive nicety.
    let mut sum_active_e9: i128 = 0;
    let mut sum_bench_e9: i128 = 0;
    let mut sum_cash_wt_e9: i128 = 0;
    for s in &window.samples {
        let active = i128::from(s.book_return_e9).checked_sub(i128::from(s.bench_return_e9))?;
        sum_active_e9 = sum_active_e9.checked_add(active)?;
        sum_bench_e9 = sum_bench_e9.checked_add(i128::from(s.bench_return_e9))?;
        sum_cash_wt_e9 = sum_cash_wt_e9.checked_add(i128::from(s.cash_weight_e9))?;
    }
    let mean_active_e9 = sum_active_e9.checked_div(n_i128)?;
    let mean_cash_wt_e9 = sum_cash_wt_e9.checked_div(n_i128)?;

    // Pass 2: sample-variance numerator, Sigma(a_i - mean)^2. `dev = active -
    // mean_active` can itself reach ~3.7e19 (both terms bounded by the
    // ~1.845e19 figure above, worst case opposite signs), so a SINGLE
    // squared term `dev * dev` can reach ~1.4e39 -- past `i128::MAX`
    // (~1.7014e38). This is not a hypothetical: extreme-but-representable
    // `i64` inputs (see `full_range_sweep_never_panics` below) genuinely
    // drive this term past `i128`, and `checked_mul` returning `None` there
    // is load-bearing for this function's total-ness, not belt-and-braces.
    let mut sum_sq_dev_e18: i128 = 0;
    for s in &window.samples {
        let active = i128::from(s.book_return_e9).checked_sub(i128::from(s.bench_return_e9))?;
        let dev = active.checked_sub(mean_active_e9)?;
        let sq = dev.checked_mul(dev)?;
        sum_sq_dev_e18 = sum_sq_dev_e18.checked_add(sq)?;
    }
    let n_minus_1 = n_i128.checked_sub(1)?; // n >= 2 guarantees this is >= 1
    let variance_e18 = sum_sq_dev_e18.checked_div(n_minus_1)?;
    // A sum of squares computed entirely with `checked_*` (never wraps) is
    // never negative -- safe to reinterpret as unsigned for `isqrt`.
    let variance_e18_u128 = u128::try_from(variance_e18).ok()?;
    let stdev_e9_u128 = variance_e18_u128.isqrt();

    let sqrt_periods_e9 = sqrt_periods_e9(TRADING_DAYS_PER_YEAR)?;
    let te_ann_e9_u128 = stdev_e9_u128
        .checked_mul(sqrt_periods_e9)?
        .checked_div(1_000_000_000u128)?;
    let tracking_error_ann_e9 = i64::try_from(te_ann_e9_u128).ok()?;

    let tracking_diff_e9 = i64::try_from(sum_active_e9).ok()?;
    let cash_weight_e9 = i64::try_from(mean_cash_wt_e9).ok()?;

    let cash_ret_e9 = i128::from(yield_annual_e9)
        .checked_mul(n_i128)?
        .checked_div(i128::from(TRADING_DAYS_PER_YEAR))?;
    let cash_drag_raw = mean_cash_wt_e9
        .checked_mul(sum_bench_e9.checked_sub(cash_ret_e9)?)?
        .checked_div(1_000_000_000i128)?;
    let cash_drag_e9 = i64::try_from(cash_drag_raw).ok()?;

    Some(TrackerRecord {
        book,
        tracking_error_ann_e9,
        tracking_diff_e9,
        cash_weight_e9,
        cash_drag_e9,
        n_obs: u32::try_from(n).ok()?,
        window_start_ns: first.window_end_ns,
        window_end_ns: last.window_end_ns,
    })
}

#[cfg(test)]
#[allow(clippy::unwrap_used)] // tests: unwrap_used/expect_used are hot-path-only bans (delta-one/CLAUDE.md)
mod tests {
    use super::*;
    use proptest::prelude::*;

    #[test]
    fn period_return_matches_hand_computation() {
        // 100.0 -> 110.0 is a +10% return.
        assert_eq!(
            period_return_e9(100_000_000_000, 110_000_000_000),
            Some(100_000_000)
        );
    }

    #[test]
    fn period_return_zero_prev_is_none() {
        assert_eq!(period_return_e9(0, 100), None);
    }

    #[test]
    fn period_return_overflow_is_none() {
        // (i64::MAX - 1) * 1e9 does not fit back into i64 once divided by 1.
        assert_eq!(period_return_e9(1, i64::MAX), None);
    }

    #[test]
    fn sqrt_periods_e9_matches_sqrt_252() {
        // Hand-verified (python `math.isqrt(252 * 10**18)`): 15_874_507_866.
        assert_eq!(sqrt_periods_e9(TRADING_DAYS_PER_YEAR), Some(15_874_507_866));
        // Sanity check on the identity itself: isqrt(1 * 1e18) == 1e9 exactly.
        assert_eq!(sqrt_periods_e9(1), Some(1_000_000_000));
    }

    #[test]
    fn analytics_is_none_below_two_observations() {
        let mut window = TrackerWindow::new(10);
        assert_eq!(analytics(BookId(1), &window, 40_000_000), None);

        window.push(Sample {
            window_end_ns: 1_000,
            book_return_e9: 10_000_000,
            bench_return_e9: 8_000_000,
            cash_weight_e9: 100_000_000,
        });
        assert_eq!(
            analytics(BookId(1), &window, 40_000_000),
            None,
            "n_obs == 1 must still be None (n - 1 == 0 denominator)"
        );
    }

    #[test]
    fn zero_active_return_gives_zero_te_and_td() {
        let mut window = TrackerWindow::new(10);
        // Book return exactly equals benchmark return on every sample -> the
        // active-return series is identically zero.
        window.push(Sample {
            window_end_ns: 1_000,
            book_return_e9: 10_000_000,
            bench_return_e9: 10_000_000,
            cash_weight_e9: 50_000_000,
        });
        window.push(Sample {
            window_end_ns: 2_000,
            book_return_e9: 20_000_000,
            bench_return_e9: 20_000_000,
            cash_weight_e9: 50_000_000,
        });

        let record = analytics(BookId(1), &window, 40_000_000).unwrap();
        assert_eq!(record.tracking_error_ann_e9, 0);
        assert_eq!(record.tracking_diff_e9, 0);
    }

    #[test]
    fn known_two_observation_window_matches_hand_computation() {
        // Hand-computed vector (verified with python `math.isqrt`):
        //   sample1: book=10_000_000 (1.0%), bench=4_000_000 (0.4%), cash_wt=100_000_000 (10%)
        //   sample2: book=20_000_000 (2.0%), bench=8_000_000 (0.8%), cash_wt=100_000_000 (10%)
        //   active1 = 6_000_000, active2 = 12_000_000
        //   TD = sum(active) = 18_000_000
        //   mean(active) = 9_000_000; dev = -3_000_000 / +3_000_000
        //   sum_sq_dev_e18 = 2 * 3_000_000^2 = 18_000_000_000_000
        //   variance_e18 (n-1=1) = 18_000_000_000_000
        //   stdev_e9 = isqrt(18_000_000_000_000) = 4_242_640
        //   sqrt(252)_e9 = 15_874_507_866
        //   TE_ann_e9 = 4_242_640 * 15_874_507_866 / 1e9 = 67_349_822
        //   cash_ret_e9 = 40_000_000 * 2 / 252 = 317_460 (truncated)
        //   sum_bench = 12_000_000
        //   cash_drag_e9 = 100_000_000 * (12_000_000 - 317_460) / 1e9 = 1_168_254
        let mut window = TrackerWindow::new(10);
        window.push(Sample {
            window_end_ns: 1_000_000_000,
            book_return_e9: 10_000_000,
            bench_return_e9: 4_000_000,
            cash_weight_e9: 100_000_000,
        });
        window.push(Sample {
            window_end_ns: 2_000_000_000,
            book_return_e9: 20_000_000,
            bench_return_e9: 8_000_000,
            cash_weight_e9: 100_000_000,
        });

        let record = analytics(BookId(7), &window, 40_000_000).unwrap();
        assert_eq!(record.book, BookId(7));
        assert_eq!(record.n_obs, 2);
        assert_eq!(record.window_start_ns, 1_000_000_000);
        assert_eq!(record.window_end_ns, 2_000_000_000);
        assert_eq!(record.tracking_diff_e9, 18_000_000);
        assert_eq!(record.tracking_error_ann_e9, 67_349_822);
        assert_eq!(record.cash_weight_e9, 100_000_000);
        assert_eq!(record.cash_drag_e9, 1_168_254);
    }

    #[test]
    fn window_evicts_oldest_past_capacity() {
        let mut window = TrackerWindow::new(2);
        for i in 0..5u64 {
            window.push(Sample {
                window_end_ns: i,
                book_return_e9: 0,
                bench_return_e9: 0,
                cash_weight_e9: 0,
            });
        }
        assert_eq!(window.len(), 2);
        // Oldest two survivors are the last two pushed: ns=3, ns=4.
        assert_eq!(analytics(BookId(1), &window, 0).unwrap().window_start_ns, 3);
        assert_eq!(analytics(BookId(1), &window, 0).unwrap().window_end_ns, 4);
    }

    /// Bounded strategy for return/weight fields: far below `i64::MAX` so
    /// `Sigma` over a handful of observations stays well inside `i128`,
    /// letting these property tests assert on `Some(record)` rather than
    /// tolerating `None` from the overflow guard.
    fn bounded_e9() -> impl Strategy<Value = i64> {
        -1_000_000_000i64..=1_000_000_000i64
    }

    proptest! {
        #[test]
        fn tracking_error_is_never_negative(
            samples in prop::collection::vec(
                (bounded_e9(), bounded_e9(), bounded_e9()),
                2..20,
            ),
            yield_annual_e9 in bounded_e9(),
        ) {
            let mut window = TrackerWindow::new(250);
            for (i, (book, bench, cash_wt)) in samples.iter().enumerate() {
                window.push(Sample {
                    window_end_ns: i as u64,
                    book_return_e9: *book,
                    bench_return_e9: *bench,
                    cash_weight_e9: *cash_wt,
                });
            }
            if let Some(record) = analytics(BookId(1), &window, yield_annual_e9) {
                prop_assert!(record.tracking_error_ann_e9 >= 0);
            }
        }
    }

    proptest! {
        #[test]
        fn tracking_error_is_zero_when_nav_tracks_bench_exactly(
            returns in prop::collection::vec(bounded_e9(), 2..20),
            cash_wt in bounded_e9(),
            yield_annual_e9 in bounded_e9(),
        ) {
            let mut window = TrackerWindow::new(250);
            for (i, r) in returns.iter().enumerate() {
                window.push(Sample {
                    window_end_ns: i as u64,
                    book_return_e9: *r,
                    bench_return_e9: *r, // book == bench every sample -> active == 0
                    cash_weight_e9: cash_wt,
                });
            }
            if let Some(record) = analytics(BookId(1), &window, yield_annual_e9) {
                prop_assert_eq!(record.tracking_error_ann_e9, 0);
                prop_assert_eq!(record.tracking_diff_e9, 0);
            }
        }
    }

    proptest! {
        #[test]
        fn tracking_diff_equals_sum_of_active_returns(
            samples in prop::collection::vec(
                (bounded_e9(), bounded_e9(), bounded_e9()),
                2..20,
            ),
            yield_annual_e9 in bounded_e9(),
        ) {
            let mut window = TrackerWindow::new(250);
            let mut expected_td: i128 = 0;
            for (i, (book, bench, cash_wt)) in samples.iter().enumerate() {
                expected_td += i128::from(*book) - i128::from(*bench);
                window.push(Sample {
                    window_end_ns: i as u64,
                    book_return_e9: *book,
                    bench_return_e9: *bench,
                    cash_weight_e9: *cash_wt,
                });
            }
            if let Some(record) = analytics(BookId(1), &window, yield_annual_e9) {
                prop_assert_eq!(i128::from(record.tracking_diff_e9), expected_td);
            }
        }
    }

    proptest! {
        #[test]
        fn analytics_is_none_below_two_observations_prop(
            push_one in any::<bool>(),
            sample in (bounded_e9(), bounded_e9(), bounded_e9()),
            yield_annual_e9 in bounded_e9(),
        ) {
            let mut window = TrackerWindow::new(250);
            if push_one {
                window.push(Sample {
                    window_end_ns: 1,
                    book_return_e9: sample.0,
                    bench_return_e9: sample.1,
                    cash_weight_e9: sample.2,
                });
            }
            prop_assert_eq!(analytics(BookId(1), &window, yield_annual_e9), None);
        }
    }

    proptest! {
        #[test]
        fn window_never_exceeds_capacity_and_end_ns_is_monotone(
            capacity in 1usize..20,
            n_pushes in 1u64..100,
        ) {
            let mut window = TrackerWindow::new(capacity);
            for ns in 0..n_pushes {
                window.push(Sample {
                    window_end_ns: ns,
                    book_return_e9: 0,
                    bench_return_e9: 0,
                    cash_weight_e9: 0,
                });
                prop_assert!(window.len() <= capacity);
                // The most recently pushed sample is always the back of the
                // window (only the front is ever evicted), so its
                // `window_end_ns` is always the just-pushed value -- the
                // window's contents are monotone in push order.
                prop_assert_eq!(window.samples.back().unwrap().window_end_ns, ns);
            }
        }
    }

    proptest! {
        #![proptest_config(ProptestConfig::with_cases(512))]
        #[test]
        fn full_range_sweep_never_panics(
            samples in prop::collection::vec(
                (any::<i64>(), any::<i64>(), any::<i64>()),
                2..8,
            ),
            yield_annual_e9 in any::<i64>(),
        ) {
            // Extreme, not-necessarily-physical i64 inputs across the full
            // domain -- the only claim under test is that `analytics` never
            // panics, returning `None` instead when the checked i128/u128
            // arithmetic would otherwise overflow.
            let mut window = TrackerWindow::new(250);
            for (i, (book, bench, cash_wt)) in samples.iter().enumerate() {
                window.push(Sample {
                    window_end_ns: i as u64,
                    book_return_e9: *book,
                    bench_return_e9: *bench,
                    cash_weight_e9: *cash_wt,
                });
            }
            let _ = analytics(BookId(1), &window, yield_annual_e9);
        }
    }

    proptest! {
        #![proptest_config(ProptestConfig::with_cases(512))]
        #[test]
        fn period_return_full_range_sweep_never_panics(prev in any::<i64>(), now in any::<i64>()) {
            let _ = period_return_e9(prev, now);
        }
    }
}
