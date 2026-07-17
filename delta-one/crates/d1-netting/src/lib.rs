//! Firm-wide netting engine (ADR-005): per-instrument, per-cycle, turns
//! per-book residual demands into explicit internal crosses plus one net
//! external order intent. Pure logic only — no proto, no NATS/Kafka, no
//! `d1-core::target::target_to_order` wiring (that stays untouched; Slice 2
//! deletes it). See delta-one/CLAUDE.md for the crate's role and hot-path
//! rules: this crate is **on** the hot path.

use std::fmt;
use std::str::FromStr;

use d1_core::BookId;

/// Upper bound on books considered in a single netting cycle. Not arbitrary:
/// books come from `protocol/refdata/universe.json` and there are 5 today, so
/// 16 is generous headroom that still lets `net` use fixed-size stack arrays
/// instead of allocating (hot-path rule #1). Raise it (and re-bench) if the
/// book count ever approaches it.
pub const MAX_BOOKS: usize = 16;

/// Cross reference price policy, stamped on every cross record (ADR-005 §4:
/// compliance-visible, never hardcoded).
// ponytail: one variant today. Add `ExecVwap` when ADR-005 §29's deliberately
// deferred open question (cross at cycle start vs after external execution
// completes) is resolved with compliance — `ExecVwap` needs the concurrent
// external residual's execution price, which is unknowable at cycle start
// and so cannot be implemented today regardless. ADR-005 §4 also specifies
// the policy "per instrument class", but no such per-class map exists in
// `protocol/refdata/universe.json` yet — today it is one policy for the
// whole cycle, passed in by the caller (`d1.toml`'s `[netting]` config per
// docs/ROADMAP.md); the upgrade path is a `InstrumentClass -> RefPxPolicy`
// lookup once that refdata exists.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RefPxPolicy {
    /// Arrival mid at netting-cycle start (ADR-005 §4 default).
    ArrivalMid,
}

impl fmt::Display for RefPxPolicy {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            RefPxPolicy::ArrivalMid => f.write_str("ARRIVAL_MID"),
        }
    }
}

impl FromStr for RefPxPolicy {
    type Err = ParseRefPxPolicyError;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s {
            "ARRIVAL_MID" => Ok(RefPxPolicy::ArrivalMid),
            _ => Err(ParseRefPxPolicyError),
        }
    }
}

/// An unrecognized ref-px policy string. `d1` propagates this with `?` at
/// startup so a typo'd `[netting]` policy config kills the process instead
/// of silently defaulting and mispricing internal risk transfers between
/// books.
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
#[error("unknown ref px policy (expected \"ARRIVAL_MID\")")]
pub struct ParseRefPxPolicyError;

/// One book's residual demand for one instrument in one netting cycle
/// (ADR-005 §1: `demand_b = target_b - position_b - inflight_b`, computed by
/// the caller; this engine only consumes the result).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BookDemand {
    /// Book this demand belongs to.
    pub book: BookId,
    /// Residual demand, units-e2. Positive = net buy, negative = net sell,
    /// zero = no demand this cycle.
    pub demand_e2: i64,
    /// No-trade band for this book/instrument this cycle, units-e2. Must be
    /// `>= 0` — this is EXO wire input (`TargetPosition.band_qty_e2` per
    /// docs/ROADMAP.md), a trust boundary.
    pub band_e2: i64,
}

/// One internal cross leg pair: `buy_book` buys `qty_e2` from `sell_book` at
/// `px_e9`. `cross_id` is minted off hot path (UUIDv7) by a later slice, not
/// here.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Cross {
    /// Book on the buy side of this internal cross.
    pub buy_book: BookId,
    /// Book on the sell side of this internal cross.
    pub sell_book: BookId,
    /// Quantity crossed, units-e2. Always `> 0`.
    pub qty_e2: i64,
    /// Cross reference price, fixed-point ×10⁹.
    pub px_e9: i64,
    /// Policy that produced `px_e9`, stamped for compliance lineage.
    pub policy: RefPxPolicy,
}

/// Result of one netting cycle for one instrument.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Netted {
    /// Net external order quantity, units-e2, after band suppression.
    /// Positive = buy, negative = sell, zero = no external order.
    pub net_external_e2: i64,
    /// Number of populated entries at the front of the caller's
    /// `out_crosses` buffer.
    pub n_crosses: usize,
    /// No-trade band applied to `net_external_e2` this cycle: `min(band_b)`
    /// over books with nonzero demand, or zero if no book had demand.
    pub band_e2: i64,
    /// Whether `net_external_e2` was suppressed to zero by `band_e2`. Never
    /// affects crosses (ADR-005 §2 puts the band on the external order
    /// only).
    pub external_suppressed: bool,
}

/// Failure modes for `net` (`d1-netting`'s one `thiserror` enum per crate,
/// delta-one/CLAUDE.md Rust guardrails).
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum NettingError {
    /// A quantity computation overflowed `i64`. Never emit a corrupted
    /// order.
    #[error("quantity overflow")]
    Overflow,
    /// More books were passed than `MAX_BOOKS` supports.
    #[error("too many books: got {got}, max {max}")]
    TooManyBooks {
        /// Number of demands passed in.
        got: usize,
        /// `MAX_BOOKS`.
        max: usize,
    },
    /// `out_crosses` ran out of room. Never silently truncate: a dropped
    /// cross leg is a silently unbooked risk transfer between books (root
    /// CLAUDE.md invariant #2).
    #[error("cross buffer full: capacity {capacity}")]
    CrossBufferFull {
        /// Length of the caller-provided `out_crosses` buffer.
        capacity: usize,
    },
    /// A book's `band_e2` was negative. This is EXO wire input; validated at
    /// the trust boundary rather than clamped silently.
    #[error("invalid band for book {book:?}: must be >= 0")]
    InvalidBand {
        /// The offending book.
        book: BookId,
    },
}

/// Net per-book demands for one instrument into internal crosses plus one
/// external order intent (ADR-005 §§1-3).
///
/// `out_crosses` is a caller-owned buffer the engine writes into (no
/// allocation, hot-path rule #1); its first `Netted::n_crosses` entries are
/// populated.
pub fn net(
    demands: &[BookDemand],
    ref_px_e9: i64,
    policy: RefPxPolicy,
    out_crosses: &mut [Cross],
) -> Result<Netted, NettingError> {
    if demands.len() > MAX_BOOKS {
        return Err(NettingError::TooManyBooks {
            got: demands.len(),
            max: MAX_BOOKS,
        });
    }

    for d in demands {
        if d.band_e2 < 0 {
            return Err(NettingError::InvalidBand { book: d.book });
        }
    }

    // Step 1: net_external pre-band (ADR-005 §1-2).
    let mut net_external_e2: i64 = 0;
    for d in demands {
        net_external_e2 = net_external_e2
            .checked_add(d.demand_e2)
            .ok_or(NettingError::Overflow)?;
    }

    // No-trade band: min(band_b) over books with nonzero demand this cycle.
    //
    // ponytail: `min` is a deliberate ceiling, not the principled rule.
    // ADR-005 §2 puts the band on the *instrument*, but no such refdata
    // field exists (`protocol/refdata/universe.json` has none); docs/ROADMAP.md:12
    // says it comes from `TargetPosition.band_qty_e2`, per-book and
    // per-message — what this engine implements; exo/CLAUDE.md:30 says that
    // wire field is merely informational (display, "why a target moved"),
    // not authoritative for trading decisions. ADR-005 §2's wording likely
    // needs an amendment to reconcile these three sources. Until then, `min`
    // is chosen because an over-wide band is a **risk** failure (the hedge
    // silently doesn't happen) while an over-tight one is only a bounded
    // **cost** failure (extra spread); `min` is never wider than the
    // principled per-book-indifference rule, so it can only over-trade, not
    // skip a needed trade. Upgrade path: once band provenance is resolved,
    // replace with whatever the instrument-level (or reconciled) rule turns
    // out to be.
    let mut band_e2: Option<i64> = None;
    for d in demands {
        if d.demand_e2 != 0 {
            band_e2 = Some(match band_e2 {
                None => d.band_e2,
                Some(current) => current.min(d.band_e2),
            });
        }
    }
    let band_e2 = band_e2.unwrap_or(0);

    // Step 2: match opposite demands, largest-opposite-first, deterministic
    // tie-break by book_id (ADR-005 §23). Fixed-size stack arrays, no Vec.
    let mut longs: [(BookId, i64); MAX_BOOKS] = [(BookId(0), 0); MAX_BOOKS];
    let mut n_longs = 0usize;
    let mut shorts: [(BookId, i64); MAX_BOOKS] = [(BookId(0), 0); MAX_BOOKS];
    let mut n_shorts = 0usize;

    for d in demands {
        if d.demand_e2 > 0 {
            if let Some(slot) = longs.get_mut(n_longs) {
                *slot = (d.book, d.demand_e2);
                n_longs += 1;
            }
        } else if d.demand_e2 < 0 {
            if let Some(slot) = shorts.get_mut(n_shorts) {
                *slot = (d.book, d.demand_e2.abs());
                n_shorts += 1;
            }
        }
    }

    let cmp_desc_qty_then_book =
        |a: &(BookId, i64), b: &(BookId, i64)| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0));
    if let Some(slice) = longs.get_mut(..n_longs) {
        slice.sort_unstable_by(cmp_desc_qty_then_book);
    }
    if let Some(slice) = shorts.get_mut(..n_shorts) {
        slice.sort_unstable_by(cmp_desc_qty_then_book);
    }

    let mut n_crosses = 0usize;
    let mut i = 0usize;
    let mut j = 0usize;
    while i < n_longs && j < n_shorts {
        let &(long_book, long_left) = longs.get(i).ok_or(NettingError::Overflow)?;
        let &(short_book, short_left) = shorts.get(j).ok_or(NettingError::Overflow)?;

        let cross_qty = long_left.min(short_left);
        let capacity = out_crosses.len();
        let slot = out_crosses
            .get_mut(n_crosses)
            .ok_or(NettingError::CrossBufferFull { capacity })?;
        *slot = Cross {
            buy_book: long_book,
            sell_book: short_book,
            qty_e2: cross_qty,
            px_e9: ref_px_e9,
            policy,
        };
        n_crosses += 1;

        let new_long_left = long_left
            .checked_sub(cross_qty)
            .ok_or(NettingError::Overflow)?;
        let new_short_left = short_left
            .checked_sub(cross_qty)
            .ok_or(NettingError::Overflow)?;
        if let Some(entry) = longs.get_mut(i) {
            entry.1 = new_long_left;
        }
        if let Some(entry) = shorts.get_mut(j) {
            entry.1 = new_short_left;
        }
        if new_long_left == 0 {
            i += 1;
        }
        if new_short_left == 0 {
            j += 1;
        }
    }

    // Step 3: no-trade band gates net_external only, never crosses (ADR-005
    // §2: the band is on the external order; crosses pay no spread, so
    // banding them would only leave books mismatched with each other for no
    // saving).
    let external_suppressed = net_external_e2.abs() <= band_e2;
    let net_external_e2 = if external_suppressed {
        0
    } else {
        net_external_e2
    };

    Ok(Netted {
        net_external_e2,
        n_crosses,
        band_e2,
        external_suppressed,
    })
}

#[cfg(test)]
#[allow(clippy::unwrap_used)] // tests: unwrap_used/expect_used are hot-path-only bans (delta-one/CLAUDE.md)
mod tests {
    use super::*;
    use proptest::prelude::*;

    fn demand(book: u32, demand_e2: i64, band_e2: i64) -> BookDemand {
        BookDemand {
            book: BookId(book),
            demand_e2,
            band_e2,
        }
    }

    #[test]
    fn adr005_worked_example() {
        // book D1-CORE +1,000,000, book EXO-SP -800,000 -> one cross of
        // 800,000 (D1-CORE buys from EXO-SP) + external buy 200,000.
        let demands = [demand(1, 1_000_000, 0), demand(2, -800_000, 0)];
        let mut out = [Cross {
            buy_book: BookId(0),
            sell_book: BookId(0),
            qty_e2: 0,
            px_e9: 0,
            policy: RefPxPolicy::ArrivalMid,
        }; 4];
        let netted = net(&demands, 150_000_000_000, RefPxPolicy::ArrivalMid, &mut out).unwrap();

        assert_eq!(netted.n_crosses, 1);
        assert_eq!(out[0].buy_book, BookId(1));
        assert_eq!(out[0].sell_book, BookId(2));
        assert_eq!(out[0].qty_e2, 800_000);
        assert_eq!(out[0].px_e9, 150_000_000_000);
        assert_eq!(netted.net_external_e2, 200_000);
        assert!(!netted.external_suppressed);
    }

    #[test]
    fn single_book_no_cross() {
        let demands = [demand(1, 500, 0)];
        let mut out = [Cross {
            buy_book: BookId(0),
            sell_book: BookId(0),
            qty_e2: 0,
            px_e9: 0,
            policy: RefPxPolicy::ArrivalMid,
        }; 4];
        let netted = net(&demands, 1, RefPxPolicy::ArrivalMid, &mut out).unwrap();
        assert_eq!(netted.n_crosses, 0);
        assert_eq!(netted.net_external_e2, 500);
    }

    #[test]
    fn all_same_side_no_crosses() {
        let demands = [demand(1, 500, 0), demand(2, 300, 0), demand(3, 200, 0)];
        let mut out = [Cross {
            buy_book: BookId(0),
            sell_book: BookId(0),
            qty_e2: 0,
            px_e9: 0,
            policy: RefPxPolicy::ArrivalMid,
        }; 4];
        let netted = net(&demands, 1, RefPxPolicy::ArrivalMid, &mut out).unwrap();
        assert_eq!(netted.n_crosses, 0);
        assert_eq!(netted.net_external_e2, 1_000);
    }

    #[test]
    fn exact_offset_zero_external() {
        let demands = [demand(1, 500_000, 0), demand(2, -500_000, 0)];
        let mut out = [Cross {
            buy_book: BookId(0),
            sell_book: BookId(0),
            qty_e2: 0,
            px_e9: 0,
            policy: RefPxPolicy::ArrivalMid,
        }; 4];
        let netted = net(&demands, 1, RefPxPolicy::ArrivalMid, &mut out).unwrap();
        assert_eq!(netted.n_crosses, 1);
        assert_eq!(out[0].qty_e2, 500_000);
        assert_eq!(netted.net_external_e2, 0);
    }

    #[test]
    fn band_suppresses_external_not_crosses() {
        // net_external pre-band = 200_000, band = 300_000 -> suppressed, but
        // the 800_000 cross still must happen.
        let demands = [demand(1, 1_000_000, 300_000), demand(2, -800_000, 300_000)];
        let mut out = [Cross {
            buy_book: BookId(0),
            sell_book: BookId(0),
            qty_e2: 0,
            px_e9: 0,
            policy: RefPxPolicy::ArrivalMid,
        }; 4];
        let netted = net(&demands, 1, RefPxPolicy::ArrivalMid, &mut out).unwrap();
        assert!(netted.external_suppressed);
        assert_eq!(netted.net_external_e2, 0);
        assert!(netted.n_crosses > 0);
        assert_eq!(out[0].qty_e2, 800_000);
    }

    #[test]
    fn band_boundary_equal_is_suppressed() {
        let demands = [demand(1, 200_000, 200_000)];
        let mut out = [Cross {
            buy_book: BookId(0),
            sell_book: BookId(0),
            qty_e2: 0,
            px_e9: 0,
            policy: RefPxPolicy::ArrivalMid,
        }; 4];
        let netted = net(&demands, 1, RefPxPolicy::ArrivalMid, &mut out).unwrap();
        assert!(netted.external_suppressed);
        assert_eq!(netted.net_external_e2, 0);
    }

    #[test]
    fn ref_px_policy_round_trips() {
        assert_eq!(RefPxPolicy::ArrivalMid.to_string(), "ARRIVAL_MID");
        assert_eq!(
            "ARRIVAL_MID".parse::<RefPxPolicy>().unwrap(),
            RefPxPolicy::ArrivalMid
        );
    }

    #[test]
    fn unknown_policy_string_errors() {
        assert_eq!(
            "EXEC_VWAP".parse::<RefPxPolicy>(),
            Err(ParseRefPxPolicyError)
        );
        assert_eq!("".parse::<RefPxPolicy>(), Err(ParseRefPxPolicyError));
    }

    #[test]
    fn overflow_errors() {
        let demands = [demand(1, i64::MAX, 0), demand(2, 1, 0)];
        let mut out = [Cross {
            buy_book: BookId(0),
            sell_book: BookId(0),
            qty_e2: 0,
            px_e9: 0,
            policy: RefPxPolicy::ArrivalMid,
        }; 4];
        let err = net(&demands, 1, RefPxPolicy::ArrivalMid, &mut out).unwrap_err();
        assert_eq!(err, NettingError::Overflow);
    }

    #[test]
    fn cross_buffer_too_small_errors() {
        let demands = [demand(1, 500_000, 0), demand(2, -500_000, 0)];
        let mut out: [Cross; 0] = [];
        let err = net(&demands, 1, RefPxPolicy::ArrivalMid, &mut out).unwrap_err();
        assert_eq!(err, NettingError::CrossBufferFull { capacity: 0 });
    }

    #[test]
    fn negative_band_errors() {
        let demands = [demand(1, 500, -1)];
        let mut out = [Cross {
            buy_book: BookId(0),
            sell_book: BookId(0),
            qty_e2: 0,
            px_e9: 0,
            policy: RefPxPolicy::ArrivalMid,
        }; 4];
        let err = net(&demands, 1, RefPxPolicy::ArrivalMid, &mut out).unwrap_err();
        assert_eq!(err, NettingError::InvalidBand { book: BookId(1) });
    }

    #[test]
    fn too_many_books_errors() {
        let demands: Vec<BookDemand> = (0..(MAX_BOOKS as u32 + 1))
            .map(|i| demand(i, 1, 0))
            .collect();
        let mut out = [Cross {
            buy_book: BookId(0),
            sell_book: BookId(0),
            qty_e2: 0,
            px_e9: 0,
            policy: RefPxPolicy::ArrivalMid,
        }; 4];
        let err = net(&demands, 1, RefPxPolicy::ArrivalMid, &mut out).unwrap_err();
        assert_eq!(
            err,
            NettingError::TooManyBooks {
                got: MAX_BOOKS + 1,
                max: MAX_BOOKS
            }
        );
    }

    #[test]
    fn multi_book_largest_opposite_first() {
        // Longs: book 3 = 600k, book 1 = 400k (descending qty, tie-break
        // wouldn't apply here since qtys differ). Shorts: book 5 = 500k,
        // book 2 = 500k (equal qty -> tie-break by ascending book_id: book 2
        // before book 5).
        let demands = [
            demand(1, 400_000, 0),
            demand(3, 600_000, 0),
            demand(5, -500_000, 0),
            demand(2, -500_000, 0),
        ];
        let mut out = [Cross {
            buy_book: BookId(0),
            sell_book: BookId(0),
            qty_e2: 0,
            px_e9: 0,
            policy: RefPxPolicy::ArrivalMid,
        }; 4];
        let netted = net(&demands, 1, RefPxPolicy::ArrivalMid, &mut out).unwrap();

        // Largest long (book 3, 600k) matches largest-tie-broken short
        // first: shorts are equal at 500k each, so book 2 (lower id) sorts
        // first.
        assert_eq!(out[0].buy_book, BookId(3));
        assert_eq!(out[0].sell_book, BookId(2));
        assert_eq!(out[0].qty_e2, 500_000);

        // Book 3 has 100k left, matches next short (book 5).
        assert_eq!(out[1].buy_book, BookId(3));
        assert_eq!(out[1].sell_book, BookId(5));
        assert_eq!(out[1].qty_e2, 100_000);

        // Book 5 has 400k left, matches remaining long (book 1, 400k) exactly.
        assert_eq!(out[2].buy_book, BookId(1));
        assert_eq!(out[2].sell_book, BookId(5));
        assert_eq!(out[2].qty_e2, 400_000);

        assert_eq!(netted.n_crosses, 3);
        assert_eq!(netted.net_external_e2, 0);
    }

    fn demand_strategy() -> impl Strategy<Value = Vec<BookDemand>> {
        // Book ids are assigned by index (0..len), always unique and within
        // MAX_BOOKS, so overflow and buffer-capacity errors don't fire
        // spuriously -- those paths have dedicated unit tests above.
        prop::collection::vec((-1_000_000i64..=1_000_000, 0i64..=1_000_000), 1..=8).prop_map(
            |entries| {
                entries
                    .into_iter()
                    .enumerate()
                    .map(|(i, (demand_e2, band_e2))| demand(i as u32, demand_e2, band_e2))
                    .collect()
            },
        )
    }

    proptest! {
        #[test]
        fn cross_conservation(demands in demand_strategy()) {
            // Every emitted cross has qty_e2 > 0 (buy leg qty == sell leg
            // qty is structural: `Cross` carries one shared qty for both
            // legs, so the substantive check is that total crossed volume
            // never exceeds what either side actually has to offer, and
            // exactly matches the smaller side's total (largest-opposite
            // -first matching always fully exhausts the smaller side).
            let mut out = [Cross {
                buy_book: BookId(0),
                sell_book: BookId(0),
                qty_e2: 0,
                px_e9: 0,
                policy: RefPxPolicy::ArrivalMid,
            }; MAX_BOOKS];
            let netted = net(&demands, 1, RefPxPolicy::ArrivalMid, &mut out).unwrap();

            let total_long: i64 = demands.iter().filter(|d| d.demand_e2 > 0).map(|d| d.demand_e2).sum();
            let total_short: i64 = demands.iter().filter(|d| d.demand_e2 < 0).map(|d| -d.demand_e2).sum();
            let crosses = out.get(..netted.n_crosses).unwrap_or(&[]);
            let total_crossed: i64 = crosses.iter().map(|c| c.qty_e2).sum();

            for c in crosses {
                prop_assert!(c.qty_e2 > 0);
            }
            prop_assert_eq!(total_crossed, total_long.min(total_short));
        }

        #[test]
        fn net_external_equals_sum_of_demands_pre_band(demands in demand_strategy()) {
            let mut out = [Cross {
                buy_book: BookId(0),
                sell_book: BookId(0),
                qty_e2: 0,
                px_e9: 0,
                policy: RefPxPolicy::ArrivalMid,
            }; MAX_BOOKS];
            // Zero every band so nothing gets suppressed, isolating the
            // pre-band net_external computation.
            let unbanded: Vec<BookDemand> = demands
                .iter()
                .map(|d| BookDemand { band_e2: 0, ..*d })
                .collect();
            let netted = net(&unbanded, 1, RefPxPolicy::ArrivalMid, &mut out).unwrap();
            let expected: i64 = unbanded.iter().map(|d| d.demand_e2).sum();
            prop_assert_eq!(netted.net_external_e2, expected);
        }

        #[test]
        fn reconciliation_per_book(demands in demand_strategy()) {
            // "Sigma signed cross legs == 0" is a tautology (every cross
            // contributes +qty to one book and -qty to another, so it always
            // cancels regardless of whether matching is correct) -- so this
            // asserts the stronger per-book form instead: each book's
            // demand equals its signed cross legs plus its residual
            // ("share" of net_external). Only the side with larger total
            // demand carries any residual (the two-pointer walk always
            // fully exhausts the smaller side), so every nonzero residual
            // must agree in sign with net_external and be bounded by it;
            // summed across books, residuals must reconstruct
            // net_external exactly.
            let mut out = [Cross {
                buy_book: BookId(0),
                sell_book: BookId(0),
                qty_e2: 0,
                px_e9: 0,
                policy: RefPxPolicy::ArrivalMid,
            }; MAX_BOOKS];
            let unbanded: Vec<BookDemand> = demands
                .iter()
                .map(|d| BookDemand { band_e2: 0, ..*d })
                .collect();
            let netted = net(&unbanded, 1, RefPxPolicy::ArrivalMid, &mut out).unwrap();

            let crosses = out.get(..netted.n_crosses).unwrap_or(&[]);
            let mut residual_sum = 0i64;
            for d in &unbanded {
                let mut signed = 0i64;
                for c in crosses {
                    if c.buy_book == d.book {
                        signed += c.qty_e2;
                    }
                    if c.sell_book == d.book {
                        signed -= c.qty_e2;
                    }
                }
                let residual = d.demand_e2 - signed;
                residual_sum += residual;
                if residual != 0 {
                    prop_assert_eq!(residual.signum(), netted.net_external_e2.signum());
                    prop_assert!(residual.abs() <= netted.net_external_e2.abs());
                }
            }
            prop_assert_eq!(residual_sum, netted.net_external_e2);
        }

        #[test]
        fn determinism_under_permutation(
            demands in demand_strategy(),
            seed in any::<u64>(),
        ) {
            let mut out_a = [Cross {
                buy_book: BookId(0),
                sell_book: BookId(0),
                qty_e2: 0,
                px_e9: 0,
                policy: RefPxPolicy::ArrivalMid,
            }; MAX_BOOKS];
            let netted_a = net(&demands, 1, RefPxPolicy::ArrivalMid, &mut out_a).unwrap();

            // Deterministic shuffle from the proptest-generated seed (no
            // external RNG crate needed): a simple LCG-driven Fisher-Yates.
            let mut shuffled = demands.clone();
            let mut state = seed | 1;
            for i in (1..shuffled.len()).rev() {
                state = state.wrapping_mul(6364136223846793005).wrapping_add(1);
                let j = (state >> 33) as usize % (i + 1);
                shuffled.swap(i, j);
            }

            let mut out_b = [Cross {
                buy_book: BookId(0),
                sell_book: BookId(0),
                qty_e2: 0,
                px_e9: 0,
                policy: RefPxPolicy::ArrivalMid,
            }; MAX_BOOKS];
            let netted_b = net(&shuffled, 1, RefPxPolicy::ArrivalMid, &mut out_b).unwrap();

            prop_assert_eq!(netted_a, netted_b);
            prop_assert_eq!(
                out_a.get(..netted_a.n_crosses).unwrap_or(&[]),
                out_b.get(..netted_b.n_crosses).unwrap_or(&[])
            );
        }
    }
}
