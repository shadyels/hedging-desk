//! Parses `protocol/refdata/universe.json` into the id lists `d1-core` needs.
//! `d1-core` stays JSON-free (docs/ROADMAP.md P1.M1 decision log); this crate
//! is the shared place that touches refdata JSON and injects ids via
//! `MarketData::new`/`PositionKeeper::new` — both `sim` and `d1` load
//! refdata through it (P1.M3 slice 1).

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use d1_core::{BookId, InstrumentId};
use serde::Deserialize;

/// Failure modes for loading and parsing the universe refdata file.
#[derive(Debug, thiserror::Error)]
pub enum RefdataError {
    /// The refdata file could not be read from disk.
    #[error("reading universe refdata at {path:?}")]
    Read {
        /// Path that failed to read.
        path: PathBuf,
        /// Underlying I/O failure.
        #[source]
        source: std::io::Error,
    },
    /// The refdata file's contents did not parse as the expected shape.
    #[error("parsing universe refdata at {path:?}")]
    Parse {
        /// Path that failed to parse.
        path: PathBuf,
        /// Underlying JSON failure.
        #[source]
        source: serde_json::Error,
    },
    /// A required refdata array (`books` or `instruments`) was empty. A
    /// process started against an empty universe would have zero keeper slots
    /// and silently trade nothing — fail loud instead of starting degraded.
    #[error("universe refdata at {path:?} has an empty `{field}` array")]
    Empty {
        /// Path whose array was empty.
        path: PathBuf,
        /// Which required array was empty (`books` or `instruments`).
        field: &'static str,
    },
    /// A book named a `benchmark_symbol` with no matching entry under
    /// `benchmarks`. A tracker book whose benchmark silently resolved to an
    /// empty constituent list would publish a benchmark return of exactly
    /// zero, making its tracking error a plausible-looking fiction (ADR-010).
    #[error("universe refdata at {path:?}: book {book} names unknown benchmark {symbol:?}")]
    UnknownBenchmark {
        /// Path whose benchmark reference did not resolve.
        path: PathBuf,
        /// Book that named the missing benchmark.
        book: u32,
        /// The unresolvable benchmark symbol.
        symbol: String,
    },
    /// A benchmark constituent is not in the `instruments` universe, so it
    /// would have no `MarketData` slot and therefore no price to compute a
    /// return from — same fail-loud posture as `UnknownBenchmark`.
    #[error(
        "universe refdata at {path:?}: benchmark {symbol:?} constituent {instrument_id} is not in the instrument universe"
    )]
    BenchmarkConstituentNotInUniverse {
        /// Path whose constituent did not resolve.
        path: PathBuf,
        /// Benchmark holding the dangling constituent.
        symbol: String,
        /// The instrument id that is not in `instruments`.
        instrument_id: u32,
    },
    /// A benchmark constituent's `weight_e9` is not positive, or the same
    /// instrument appears twice in one benchmark's constituent list. Either
    /// would make `d1-analytics`'s fixed-weight `Σ w_c · r_c` benchmark
    /// return (ADR-010 §3) silently wrong rather than reflecting the stated
    /// composition.
    #[error(
        "universe refdata at {path:?}: benchmark {symbol:?} has a non-positive or duplicate weight_e9 for instrument {instrument_id}"
    )]
    BenchmarkWeightInvalid {
        /// Path whose weight is invalid.
        path: PathBuf,
        /// Benchmark holding the invalid weight.
        symbol: String,
        /// The offending instrument id.
        instrument_id: u32,
    },
    /// A tracker book's `benchmark_symbol` is not itself listed as a
    /// quotable instrument in `instruments`. `d1-posttrade::convert`'s
    /// `resolve_tracker_benchmark_symbol` and `crates/d1/src/lib.rs::spawn`'s
    /// `tracker_benchmarks` both resolve `benchmark_symbol` through
    /// `symbol_to_id` -- left unchecked here, a universe.json that passes
    /// startup validation can still yield zero records on
    /// `posttrade.tracker.analytics` (discovered only at end-of-session
    /// Kafka encode time) while the NATS plane silently publishes with
    /// `benchmark` unset, same fail-loud posture as `UnknownBenchmark` and
    /// `BenchmarkConstituentNotInUniverse` above.
    #[error(
        "universe refdata at {path:?}: book {book} benchmark {symbol:?} is not itself listed as a quotable instrument"
    )]
    BenchmarkSymbolNotQuotable {
        /// Path whose benchmark symbol did not resolve as an instrument.
        path: PathBuf,
        /// Book that named the unquotable benchmark.
        book: u32,
        /// The benchmark symbol that is not itself a quotable instrument.
        symbol: String,
    },
    /// A benchmark's `weight_e9` values do not sum to `1_000_000_000` --
    /// `universe.json`'s own documented convention (PORTFOLIO weights,
    /// normalised to 1.0e9). Left unenforced, the fixed-weight benchmark
    /// return is silently scaled by whatever the weights actually sum to,
    /// corrupting every TE/TD/cash-drag figure published for that book with
    /// no error anywhere.
    #[error(
        "universe refdata at {path:?}: benchmark {symbol:?} weight_e9 values sum to {sum}, expected 1_000_000_000"
    )]
    BenchmarkWeightsDoNotSumToOne {
        /// Path whose benchmark weights are unbalanced.
        path: PathBuf,
        /// The unbalanced benchmark.
        symbol: String,
        /// The actual sum.
        sum: i64,
    },
}

#[derive(Debug, Deserialize)]
struct BookDef {
    book_id: u32,
    /// Present only on tracker books; absent books simply have no analytics.
    #[serde(default)]
    benchmark_symbol: Option<String>,
    /// Endowed cash base for a tracker book, `_e9`. Absent (or 0) means the
    /// book starts flat — legal, but every NAV return is then 0/0.
    #[serde(default, rename = "initial_cash_e9_DEMO_PLACEHOLDER")]
    initial_cash_e9: i64,
}

#[derive(Debug, Deserialize)]
struct BenchmarkDef {
    constituents: Vec<ConstituentDef>,
}

#[derive(Debug, Deserialize)]
struct ConstituentDef {
    instrument_id: u32,
    weight_e9: i64,
}

#[derive(Debug, Deserialize)]
struct InstrumentDef {
    instrument_id: u32,
    symbol: String,
    currency: String,
}

#[derive(Debug, Deserialize)]
struct ConventionsDef {
    cross_px_policy_default: String,
    venue_counterparty_default: String,
    #[serde(rename = "cash_yield_annual_e9_DEMO_PLACEHOLDER")]
    cash_yield_annual_e9: i64,
}

#[derive(Debug, Deserialize)]
struct UniverseFile {
    books: Vec<BookDef>,
    instruments: Vec<InstrumentDef>,
    conventions: ConventionsDef,
    #[serde(default)]
    benchmarks: HashMap<String, BenchmarkDef>,
}

/// One benchmark-tracking book, resolved against the instrument universe.
/// Only books carrying a `benchmark_symbol` appear (ADR-010: analytics are a
/// tracker-book concept, not a firm-wide one).
#[derive(Debug, Clone)]
pub struct TrackerBook {
    /// The tracker book itself.
    pub book: BookId,
    /// Benchmark ticker, e.g. `"SPX"`. Carried through to the Avro
    /// `benchmark_symbol` field on `posttrade.tracker.analytics`.
    pub benchmark_symbol: String,
    /// `(instrument, weight_e9)` pairs read as PORTFOLIO weights: they
    /// normalise to `1.0e9` and the benchmark return is the fixed-weight sum
    /// `Σ w_c · r_c`, implicitly rebalanced at every sample. `universe.json`
    /// does not state which reading it intends; this is the documented one.
    pub constituents: Vec<(InstrumentId, i64)>,
    /// Endowed cash base seeded into `PositionKeeper` at startup.
    pub initial_cash_e9: i64,
}

/// Book/instrument ids parsed from refdata, plus a symbol lookup for
/// resolving scenario YAML (which addresses instruments by ticker, not id).
#[derive(Debug)]
pub struct Universe {
    /// All book ids in the universe, in file order.
    pub book_ids: Vec<BookId>,
    /// All instrument ids in the universe, in file order.
    pub instrument_ids: Vec<InstrumentId>,
    /// Benchmark-tracking books, resolved against the instrument universe
    /// (ADR-010). A book without a `benchmark_symbol` simply does not
    /// appear here.
    pub tracker_books: Vec<TrackerBook>,
    /// Demo cash-yield convention, `_e9` fixed point annualized, from
    /// `conventions.cash_yield_annual_e9`. Consumed by tracker analytics
    /// (`PositionKeeper::accrue_cash`, ADR-010).
    pub cash_yield_annual_e9: i64,
    /// Ticker symbol -> instrument id.
    pub symbol_to_id: HashMap<String, InstrumentId>,
    /// Instrument id -> ticker symbol (inverse of `symbol_to_id`). Used by
    /// post-trade encoding (`d1-posttrade::convert`) to resolve the wire
    /// `symbol` field from the id-only ring payload.
    pub id_to_symbol: HashMap<InstrumentId, String>,
    /// Instrument id -> settlement currency. Used by post-trade encoding
    /// (`d1-posttrade::convert`) to resolve the wire `currency` field from
    /// the id-only ring payload.
    pub id_to_currency: HashMap<InstrumentId, String>,
    /// Cross reference-price policy id from `conventions.cross_px_policy_default`.
    /// Kept as the raw refdata string: `d1` parses it into `d1_netting::RefPxPolicy`
    /// at startup, so an unknown policy is a hard startup error rather than a
    /// silent default (ADR-005 §4 — compliance-visible, never hardcoded).
    pub cross_px_policy: String,
    /// Counterparty label for external-venue fills, from
    /// `conventions.venue_counterparty_default`. There is exactly one venue
    /// in this universe (the `sim` FIX acceptor), so this is a single
    /// refdata-sourced string, not a venue registry -- used by
    /// `d1-posttrade::convert::encode_trade` to resolve `TradeLeg`'s wire
    /// `counterparty` field for `TradeKind::ExternalFill` (internal cross
    /// legs are always `"INTERNAL"`, no lookup needed).
    pub venue_counterparty: String,
}

/// Load and parse the universe refdata file at `path`.
pub fn load(path: &Path) -> Result<Universe, RefdataError> {
    let raw = std::fs::read_to_string(path).map_err(|source| RefdataError::Read {
        path: path.to_path_buf(),
        source,
    })?;
    parse(path, &raw)
}

fn parse(path: &Path, raw: &str) -> Result<Universe, RefdataError> {
    let parsed: UniverseFile = serde_json::from_str(raw).map_err(|source| RefdataError::Parse {
        path: path.to_path_buf(),
        source,
    })?;

    if parsed.books.is_empty() {
        return Err(RefdataError::Empty {
            path: path.to_path_buf(),
            field: "books",
        });
    }
    if parsed.instruments.is_empty() {
        return Err(RefdataError::Empty {
            path: path.to_path_buf(),
            field: "instruments",
        });
    }

    let book_ids = parsed.books.iter().map(|b| BookId(b.book_id)).collect();
    let instrument_ids = parsed
        .instruments
        .iter()
        .map(|i| InstrumentId(i.instrument_id))
        .collect();
    let cross_px_policy = parsed.conventions.cross_px_policy_default;
    let venue_counterparty = parsed.conventions.venue_counterparty_default;
    let cash_yield_annual_e9 = parsed.conventions.cash_yield_annual_e9;
    let mut symbol_to_id = HashMap::with_capacity(parsed.instruments.len());
    let mut id_to_symbol = HashMap::with_capacity(parsed.instruments.len());
    let mut id_to_currency = HashMap::with_capacity(parsed.instruments.len());
    for i in parsed.instruments {
        let id = InstrumentId(i.instrument_id);
        symbol_to_id.insert(i.symbol.clone(), id);
        id_to_symbol.insert(id, i.symbol);
        id_to_currency.insert(id, i.currency);
    }

    // Only books carrying a `benchmark_symbol` produce a `TrackerBook`; a
    // book without one has no analytics and is silently absent, not an
    // error (ADR-010: analytics are a tracker-book concept, not firm-wide).
    let mut tracker_books = Vec::new();
    for b in &parsed.books {
        let Some(symbol) = &b.benchmark_symbol else {
            continue;
        };
        let benchmark =
            parsed
                .benchmarks
                .get(symbol)
                .ok_or_else(|| RefdataError::UnknownBenchmark {
                    path: path.to_path_buf(),
                    book: b.book_id,
                    symbol: symbol.clone(),
                })?;
        // The benchmark symbol must ALSO resolve through `symbol_to_id` --
        // `d1-posttrade`/`d1`'s NATS gateway both need that (see
        // `BenchmarkSymbolNotQuotable`'s doc comment), and this loop is the
        // only place with both `tracker_books` and `symbol_to_id` (built
        // above) in scope to catch it at startup.
        if !symbol_to_id.contains_key(symbol) {
            return Err(RefdataError::BenchmarkSymbolNotQuotable {
                path: path.to_path_buf(),
                book: b.book_id,
                symbol: symbol.clone(),
            });
        }
        let mut constituents = Vec::with_capacity(benchmark.constituents.len());
        let mut seen_instruments =
            std::collections::HashSet::with_capacity(benchmark.constituents.len());
        let mut weight_sum_e9: i64 = 0;
        for c in &benchmark.constituents {
            let id = InstrumentId(c.instrument_id);
            // A dangling constituent has no `MarketData` slot and therefore
            // no price to compute a return from -- fail loud rather than
            // silently dropping it from the benchmark composition.
            if !id_to_currency.contains_key(&id) {
                return Err(RefdataError::BenchmarkConstituentNotInUniverse {
                    path: path.to_path_buf(),
                    symbol: symbol.clone(),
                    instrument_id: c.instrument_id,
                });
            }
            if c.weight_e9 <= 0 || !seen_instruments.insert(c.instrument_id) {
                return Err(RefdataError::BenchmarkWeightInvalid {
                    path: path.to_path_buf(),
                    symbol: symbol.clone(),
                    instrument_id: c.instrument_id,
                });
            }
            weight_sum_e9 = weight_sum_e9.checked_add(c.weight_e9).ok_or_else(|| {
                RefdataError::BenchmarkWeightInvalid {
                    path: path.to_path_buf(),
                    symbol: symbol.clone(),
                    instrument_id: c.instrument_id,
                }
            })?;
            constituents.push((id, c.weight_e9));
        }
        if weight_sum_e9 != 1_000_000_000 {
            return Err(RefdataError::BenchmarkWeightsDoNotSumToOne {
                path: path.to_path_buf(),
                symbol: symbol.clone(),
                sum: weight_sum_e9,
            });
        }
        tracker_books.push(TrackerBook {
            book: BookId(b.book_id),
            benchmark_symbol: symbol.clone(),
            constituents,
            initial_cash_e9: b.initial_cash_e9,
        });
    }

    Ok(Universe {
        book_ids,
        instrument_ids,
        tracker_books,
        cash_yield_annual_e9,
        symbol_to_id,
        id_to_symbol,
        id_to_currency,
        cross_px_policy,
        venue_counterparty,
    })
}

#[cfg(test)]
#[allow(clippy::unwrap_used)] // tests: unwrap_used/expect_used are hot-path-only bans (delta-one/CLAUDE.md)
mod tests {
    use super::*;

    fn universe_path() -> PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../protocol/refdata/universe.json")
    }

    #[test]
    fn parses_real_universe_file() {
        let universe = load(&universe_path()).unwrap();
        assert_eq!(universe.book_ids.len(), 5);
        assert_eq!(universe.instrument_ids.len(), 15);
        assert_eq!(universe.cross_px_policy, "ARRIVAL_MID");
        assert_eq!(
            universe.symbol_to_id.get("AAPL").copied(),
            Some(InstrumentId(1001))
        );
    }

    #[test]
    fn id_to_symbol_and_currency_resolve() {
        let universe = load(&universe_path()).unwrap();
        assert_eq!(
            universe.id_to_symbol.get(&InstrumentId(1001)).cloned(),
            Some("AAPL".to_string())
        );
        assert_eq!(
            universe.id_to_currency.get(&InstrumentId(1001)).cloned(),
            Some("USD".to_string())
        );
        assert_eq!(
            universe.id_to_symbol.get(&InstrumentId(1003)).cloned(),
            Some("NESN".to_string())
        );
        assert_eq!(
            universe.id_to_currency.get(&InstrumentId(1003)).cloned(),
            Some("CHF".to_string())
        );
    }

    #[test]
    fn missing_conventions_block_is_a_parse_error() {
        let raw = r#"{
            "books": [{"book_id": 1}],
            "instruments": [{"instrument_id": 1001, "symbol": "AAPL", "currency": "USD"}]
        }"#;
        let err = parse(Path::new("test.json"), raw).unwrap_err();
        assert!(matches!(err, RefdataError::Parse { .. }));
    }

    #[test]
    fn empty_books_or_instruments_is_an_error() {
        let empty_books = r#"{
            "books": [],
            "instruments": [{"instrument_id": 1001, "symbol": "AAPL", "currency": "USD"}],
            "conventions": {"cross_px_policy_default": "ARRIVAL_MID", "venue_counterparty_default": "SIM", "cash_yield_annual_e9_DEMO_PLACEHOLDER": 40000000}
        }"#;
        assert!(matches!(
            parse(Path::new("test.json"), empty_books).unwrap_err(),
            RefdataError::Empty { field: "books", .. }
        ));

        let empty_instruments = r#"{
            "books": [{"book_id": 1}],
            "instruments": [],
            "conventions": {"cross_px_policy_default": "ARRIVAL_MID", "venue_counterparty_default": "SIM", "cash_yield_annual_e9_DEMO_PLACEHOLDER": 40000000}
        }"#;
        assert!(matches!(
            parse(Path::new("test.json"), empty_instruments).unwrap_err(),
            RefdataError::Empty {
                field: "instruments",
                ..
            }
        ));
    }

    #[test]
    fn parses_tracker_books() {
        let universe = load(&universe_path()).unwrap();
        assert_eq!(universe.tracker_books.len(), 1);
        let tracker = universe.tracker_books.first().unwrap();
        assert_eq!(tracker.book, BookId(1));
        assert_eq!(tracker.benchmark_symbol, "SPX");
        assert_eq!(
            tracker.constituents,
            vec![
                (InstrumentId(1001), 600_000_000),
                (InstrumentId(1002), 400_000_000),
            ]
        );
        assert_eq!(tracker.initial_cash_e9, 10_000_000_000_000_000);
        assert_eq!(universe.cash_yield_annual_e9, 40_000_000);
    }

    #[test]
    fn unknown_benchmark_symbol_is_an_error() {
        let raw = r#"{
            "books": [{"book_id": 1, "benchmark_symbol": "NOPE", "initial_cash_e9_DEMO_PLACEHOLDER": 100}],
            "instruments": [{"instrument_id": 1001, "symbol": "AAPL", "currency": "USD"}],
            "conventions": {"cross_px_policy_default": "ARRIVAL_MID", "venue_counterparty_default": "SIM", "cash_yield_annual_e9_DEMO_PLACEHOLDER": 40000000}
        }"#;
        let err = parse(Path::new("test.json"), raw).unwrap_err();
        assert!(matches!(
            err,
            RefdataError::UnknownBenchmark { book: 1, ref symbol, .. } if symbol == "NOPE"
        ));
    }

    #[test]
    fn benchmark_constituent_not_in_universe_is_an_error() {
        let raw = r#"{
            "books": [{"book_id": 1, "benchmark_symbol": "SPX", "initial_cash_e9_DEMO_PLACEHOLDER": 100}],
            "instruments": [
                {"instrument_id": 1001, "symbol": "AAPL", "currency": "USD"},
                {"instrument_id": 9998, "symbol": "SPX", "currency": "USD"}
            ],
            "conventions": {"cross_px_policy_default": "ARRIVAL_MID", "venue_counterparty_default": "SIM", "cash_yield_annual_e9_DEMO_PLACEHOLDER": 40000000},
            "benchmarks": {"SPX": {"constituents": [{"instrument_id": 9999, "weight_e9": 1000000000}]}}
        }"#;
        let err = parse(Path::new("test.json"), raw).unwrap_err();
        assert!(matches!(
            err,
            RefdataError::BenchmarkConstituentNotInUniverse {
                ref symbol,
                instrument_id: 9999,
                ..
            } if symbol == "SPX"
        ));
    }

    #[test]
    fn benchmark_symbol_not_quotable_is_an_error() {
        // SPX's constituents all resolve fine and weights sum to 1.0e9, but
        // "SPX" itself is never listed under `instruments` -- the missing
        // third invariant (M5): `d1-posttrade::convert` and the NATS gateway
        // both need `benchmark_symbol` to resolve through `symbol_to_id`.
        let raw = r#"{
            "books": [{"book_id": 1, "benchmark_symbol": "SPX", "initial_cash_e9_DEMO_PLACEHOLDER": 100}],
            "instruments": [{"instrument_id": 1001, "symbol": "AAPL", "currency": "USD"}],
            "conventions": {"cross_px_policy_default": "ARRIVAL_MID", "venue_counterparty_default": "SIM", "cash_yield_annual_e9_DEMO_PLACEHOLDER": 40000000},
            "benchmarks": {"SPX": {"constituents": [{"instrument_id": 1001, "weight_e9": 1000000000}]}}
        }"#;
        let err = parse(Path::new("test.json"), raw).unwrap_err();
        assert!(matches!(
            err,
            RefdataError::BenchmarkSymbolNotQuotable { book: 1, ref symbol, .. } if symbol == "SPX"
        ));
    }

    #[test]
    fn benchmark_weights_not_summing_to_one_is_an_error() {
        let raw = r#"{
            "books": [{"book_id": 1, "benchmark_symbol": "SPX", "initial_cash_e9_DEMO_PLACEHOLDER": 100}],
            "instruments": [
                {"instrument_id": 1001, "symbol": "AAPL", "currency": "USD"},
                {"instrument_id": 9998, "symbol": "SPX", "currency": "USD"}
            ],
            "conventions": {"cross_px_policy_default": "ARRIVAL_MID", "venue_counterparty_default": "SIM", "cash_yield_annual_e9_DEMO_PLACEHOLDER": 40000000},
            "benchmarks": {"SPX": {"constituents": [{"instrument_id": 1001, "weight_e9": 400000000}]}}
        }"#;
        let err = parse(Path::new("test.json"), raw).unwrap_err();
        assert!(matches!(
            err,
            RefdataError::BenchmarkWeightsDoNotSumToOne { sum: 400_000_000, ref symbol, .. } if symbol == "SPX"
        ));
    }

    #[test]
    fn benchmark_duplicate_constituent_is_an_error() {
        let raw = r#"{
            "books": [{"book_id": 1, "benchmark_symbol": "SPX", "initial_cash_e9_DEMO_PLACEHOLDER": 100}],
            "instruments": [
                {"instrument_id": 1001, "symbol": "AAPL", "currency": "USD"},
                {"instrument_id": 9998, "symbol": "SPX", "currency": "USD"}
            ],
            "conventions": {"cross_px_policy_default": "ARRIVAL_MID", "venue_counterparty_default": "SIM", "cash_yield_annual_e9_DEMO_PLACEHOLDER": 40000000},
            "benchmarks": {"SPX": {"constituents": [
                {"instrument_id": 1001, "weight_e9": 500000000},
                {"instrument_id": 1001, "weight_e9": 500000000}
            ]}}
        }"#;
        let err = parse(Path::new("test.json"), raw).unwrap_err();
        assert!(matches!(
            err,
            RefdataError::BenchmarkWeightInvalid { instrument_id: 1001, ref symbol, .. } if symbol == "SPX"
        ));
    }

    #[test]
    fn benchmark_negative_weight_is_an_error() {
        let raw = r#"{
            "books": [{"book_id": 1, "benchmark_symbol": "SPX", "initial_cash_e9_DEMO_PLACEHOLDER": 100}],
            "instruments": [
                {"instrument_id": 1001, "symbol": "AAPL", "currency": "USD"},
                {"instrument_id": 9998, "symbol": "SPX", "currency": "USD"}
            ],
            "conventions": {"cross_px_policy_default": "ARRIVAL_MID", "venue_counterparty_default": "SIM", "cash_yield_annual_e9_DEMO_PLACEHOLDER": 40000000},
            "benchmarks": {"SPX": {"constituents": [{"instrument_id": 1001, "weight_e9": -1000000000}]}}
        }"#;
        let err = parse(Path::new("test.json"), raw).unwrap_err();
        assert!(matches!(
            err,
            RefdataError::BenchmarkWeightInvalid { instrument_id: 1001, ref symbol, .. } if symbol == "SPX"
        ));
    }
}
