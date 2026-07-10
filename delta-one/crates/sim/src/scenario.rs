//! Scenario YAML parsing (sim/CLAUDE.md #3). M1 only drives `quote`/`gap`
//! actions — `exo_book_event`/`dividend`/`expect` entries parse fine (so the
//! flagship `tracker-flow.yaml` loads without error) but are ignored: no EXO
//! book, order path, or tracker analytics exist yet (docs/ROADMAP.md
//! P1.M2/P1.M3/P1.M5).

use std::path::Path;

use anyhow::{Context, Result};
use serde::Deserialize;

/// A parsed scenario file.
#[derive(Debug, Deserialize)]
pub struct Scenario {
    /// Scenario name, for logging.
    pub scenario: String,
    /// Seed for randomized modes. Unused in M1 (`replay` mode is fully
    /// scripted; no `random-walk`/`burst` modes yet).
    pub seed: u64,
    /// Path to the universe refdata file, relative to this scenario file.
    pub universe: String,
    /// Scripted timeline, in ascending `at_ms` order.
    pub timeline: Vec<TimelineEntry>,
}

/// One timeline entry. Fields are a superset across all action kinds; unused
/// fields for a given `action` are `None`.
#[derive(Debug, Deserialize)]
pub struct TimelineEntry {
    /// Offset from scenario start, milliseconds.
    pub at_ms: u64,
    /// Action kind (`quote`, `gap`, `exo_book_event`, `dividend`, ...).
    /// Absent for `expect:` assertion entries.
    pub action: Option<String>,
    /// Ticker symbol the action applies to.
    pub instrument: Option<String>,
    /// `quote` bid price (decimal, converted to fixed-point at this boundary).
    pub bid: Option<f64>,
    /// `quote` ask price (decimal, converted to fixed-point at this boundary).
    pub ask: Option<f64>,
    /// `gap` target mid price (decimal, converted to fixed-point at this boundary).
    pub to_mid: Option<f64>,
}

/// Load and parse a scenario YAML file at `path`.
pub fn load(path: &Path) -> Result<Scenario> {
    let raw = std::fs::read_to_string(path)
        .with_context(|| format!("reading scenario at {}", path.display()))?;
    serde_yaml::from_str(&raw).with_context(|| format!("parsing scenario at {}", path.display()))
}
