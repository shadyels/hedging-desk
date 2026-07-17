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
}

#[derive(Debug, Deserialize)]
struct BookDef {
    book_id: u32,
}

#[derive(Debug, Deserialize)]
struct InstrumentDef {
    instrument_id: u32,
    symbol: String,
}

#[derive(Debug, Deserialize)]
struct ConventionsDef {
    cross_px_policy_default: String,
}

#[derive(Debug, Deserialize)]
struct UniverseFile {
    books: Vec<BookDef>,
    instruments: Vec<InstrumentDef>,
    conventions: ConventionsDef,
}

/// Book/instrument ids parsed from refdata, plus a symbol lookup for
/// resolving scenario YAML (which addresses instruments by ticker, not id).
#[derive(Debug)]
pub struct Universe {
    /// All book ids in the universe, in file order.
    pub book_ids: Vec<BookId>,
    /// All instrument ids in the universe, in file order.
    pub instrument_ids: Vec<InstrumentId>,
    /// Ticker symbol -> instrument id.
    pub symbol_to_id: HashMap<String, InstrumentId>,
    /// Cross reference-price policy id from `conventions.cross_px_policy_default`.
    /// Kept as the raw refdata string: `d1` parses it into `d1_netting::RefPxPolicy`
    /// at startup, so an unknown policy is a hard startup error rather than a
    /// silent default (ADR-005 §4 — compliance-visible, never hardcoded).
    pub cross_px_policy: String,
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
    let symbol_to_id = parsed
        .instruments
        .into_iter()
        .map(|i| (i.symbol, InstrumentId(i.instrument_id)))
        .collect();

    Ok(Universe {
        book_ids,
        instrument_ids,
        symbol_to_id,
        cross_px_policy,
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
    fn missing_conventions_block_is_a_parse_error() {
        let raw = r#"{
            "books": [{"book_id": 1}],
            "instruments": [{"instrument_id": 1001, "symbol": "AAPL"}]
        }"#;
        let err = parse(Path::new("test.json"), raw).unwrap_err();
        assert!(matches!(err, RefdataError::Parse { .. }));
    }

    #[test]
    fn empty_books_or_instruments_is_an_error() {
        let empty_books = r#"{
            "books": [],
            "instruments": [{"instrument_id": 1001, "symbol": "AAPL"}],
            "conventions": {"cross_px_policy_default": "ARRIVAL_MID"}
        }"#;
        assert!(matches!(
            parse(Path::new("test.json"), empty_books).unwrap_err(),
            RefdataError::Empty { field: "books", .. }
        ));

        let empty_instruments = r#"{
            "books": [{"book_id": 1}],
            "instruments": [],
            "conventions": {"cross_px_policy_default": "ARRIVAL_MID"}
        }"#;
        assert!(matches!(
            parse(Path::new("test.json"), empty_instruments).unwrap_err(),
            RefdataError::Empty {
                field: "instruments",
                ..
            }
        ));
    }
}
