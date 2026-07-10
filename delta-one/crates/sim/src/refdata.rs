//! Parses `protocol/refdata/universe.json` into the id lists `d1-core` needs.
//! `d1-core` stays JSON-free (docs/ROADMAP.md P1.M1 decision log); sim is the
//! only place that touches refdata JSON and injects ids via
//! `MarketData::new`/`PositionKeeper::new`.

use std::collections::HashMap;
use std::path::Path;

use anyhow::{Context, Result};
use d1_core::{BookId, InstrumentId};
use serde::Deserialize;

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
struct UniverseFile {
    books: Vec<BookDef>,
    instruments: Vec<InstrumentDef>,
}

/// Book/instrument ids parsed from refdata, plus a symbol lookup for
/// resolving scenario YAML (which addresses instruments by ticker, not id).
pub struct Universe {
    /// All book ids in the universe, in file order.
    pub book_ids: Vec<BookId>,
    /// All instrument ids in the universe, in file order.
    pub instrument_ids: Vec<InstrumentId>,
    /// Ticker symbol -> instrument id.
    pub symbol_to_id: HashMap<String, InstrumentId>,
}

/// Load and parse the universe refdata file at `path`.
pub fn load(path: &Path) -> Result<Universe> {
    let raw = std::fs::read_to_string(path)
        .with_context(|| format!("reading universe refdata at {}", path.display()))?;
    let parsed: UniverseFile = serde_json::from_str(&raw)
        .with_context(|| format!("parsing universe refdata at {}", path.display()))?;

    let book_ids = parsed.books.iter().map(|b| BookId(b.book_id)).collect();
    let instrument_ids = parsed
        .instruments
        .iter()
        .map(|i| InstrumentId(i.instrument_id))
        .collect();
    let symbol_to_id = parsed
        .instruments
        .into_iter()
        .map(|i| (i.symbol, InstrumentId(i.instrument_id)))
        .collect();

    Ok(Universe {
        book_ids,
        instrument_ids,
        symbol_to_id,
    })
}
