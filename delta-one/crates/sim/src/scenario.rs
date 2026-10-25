//! Scenario YAML parsing (sim/CLAUDE.md #3). Feed-bearing actions are `quote`,
//! `gap` and `dividend` (`emit.rs`). `exo_book_event` and `tracker_flow` are
//! harness checkpoints with no tick (ADR-014 §10); `emit-ticks --script-out`
//! writes them, and the `expect:` entries, as a script for the storyline
//! harness (`crates/d1/tests/storyline.rs`). `load` rejects an unknown
//! `expect` kind and a `tracker_flow`/`exo_book_event` missing its fields.
//! A scenario's `fill_model:` block and an `exo_book_event`'s `note:` are not
//! modelled here (serde ignores unknown keys): the acceptor takes its fill
//! model from the CLI (`--fill-model`). A `tracker_flow`'s `note` IS consumed:
//! it becomes the stand-in `TargetPosition.reason` (ADR-014 §10).

use std::path::Path;

use anyhow::{Context, Result, bail};
use serde::Deserialize;

/// The `expect:` kinds the two repository scenarios use (ADR-014 §10). Any
/// other value is a parse error.
pub const KNOWN_EXPECTS: [&str; 9] = [
    "exo_targets_republished",
    "internal_cross",
    "external_residual_order",
    "tracker_analytics",
    "risk_limit_alert",
    "hedge_proposal",
    "option_orders_executed",
    "internal_transfer",
    "rates_hedge_executed",
];

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
#[derive(Debug, Default, Deserialize)]
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
    /// `dividend` per-share cash amount (decimal, converted to fixed-point
    /// at this boundary, same as `bid`/`ask`/`to_mid`).
    pub amount_per_share: Option<f64>,
    /// `expect:` kind (one of [`KNOWN_EXPECTS`]); `at_ms` is its DEADLINE.
    pub expect: Option<String>,
    /// Book code (`D1-CORE`, `EXO-SP`) for `exo_book_event` / `tracker_flow`.
    pub book: Option<String>,
    /// `tracker_flow` label, published as `reason = "tracker_flow: <note>"`
    /// (ADR-014 §10). Required and single-line: it travels as the last field
    /// of a script line.
    pub note: Option<String>,
    /// `tracker_flow` absolute target, decimal units (converted to qty_e2).
    pub target_qty: Option<f64>,
    /// `tracker_flow` no-trade band half-width, decimal units (>= 0).
    pub band_qty: Option<f64>,
}

/// Load and parse a scenario YAML file at `path`.
pub fn load(path: &Path) -> Result<Scenario> {
    let raw = std::fs::read_to_string(path)
        .with_context(|| format!("reading scenario at {}", path.display()))?;
    parse(&raw).with_context(|| format!("parsing scenario at {}", path.display()))
}

/// Parse and validate scenario YAML text.
pub(crate) fn parse(raw: &str) -> Result<Scenario> {
    let scenario: Scenario = serde_yaml::from_str(raw)?;
    for entry in &scenario.timeline {
        let at = entry.at_ms;
        if let Some(kind) = entry.expect.as_deref() {
            if !KNOWN_EXPECTS.contains(&kind) {
                bail!("expect at {at}ms: unknown kind '{kind}' (known: {KNOWN_EXPECTS:?})");
            }
        }
        match entry.action.as_deref() {
            Some("exo_book_event") if entry.book.is_none() => {
                bail!("exo_book_event at {at}ms requires `book`");
            }
            Some("tracker_flow") => {
                let (Some(_), Some(_), Some(target), Some(band), Some(note)) = (
                    &entry.book,
                    &entry.instrument,
                    entry.target_qty,
                    entry.band_qty,
                    &entry.note,
                ) else {
                    bail!(
                        "tracker_flow at {at}ms requires `book`, `instrument`, `target_qty`, `band_qty` and `note`"
                    );
                };
                if note.trim().is_empty() || note.contains(['\n', '\r']) {
                    bail!("tracker_flow at {at}ms: `note` must be non-empty and single-line");
                }
                if !target.is_finite() {
                    bail!("tracker_flow at {at}ms: target_qty {target} must be finite");
                }
                if !band.is_finite() || band < 0.0 {
                    bail!("tracker_flow at {at}ms: band_qty {band} must be finite and >= 0");
                }
            }
            _ => {}
        }
    }
    Ok(scenario)
}

#[cfg(test)]
// tests: unwrap_used/expect_used are hot-path-only bans (delta-one/CLAUDE.md).
#[allow(clippy::unwrap_used)]
mod tests {
    use super::parse;

    const HEAD: &str = "scenario: t\nseed: 1\nuniverse: u.json\ntimeline:\n";

    fn err(entries: &str) -> String {
        format!("{:#}", parse(&format!("{HEAD}{entries}")).unwrap_err())
    }

    #[test]
    fn unknown_expect_kind_errors() {
        assert!(err("  - {at_ms: 1, expect: nonsense}\n").contains("unknown kind 'nonsense'"));
        assert!(parse(&format!("{HEAD}  - {{at_ms: 1, expect: internal_cross}}\n")).is_ok());
    }

    #[test]
    fn tracker_flow_missing_band_qty_errors() {
        let e = err(
            "  - {at_ms: 1, action: tracker_flow, book: D1-CORE, instrument: AAPL, target_qty: 5, note: n}\n",
        );
        assert!(e.contains("band_qty"), "{e}");
    }

    #[test]
    fn tracker_flow_negative_band_and_book_event_without_book_error() {
        let e = err(
            "  - {at_ms: 1, action: tracker_flow, book: D1-CORE, instrument: AAPL, target_qty: 5, band_qty: -1, note: n}\n",
        );
        assert!(e.contains(">= 0"), "{e}");
        assert!(err("  - {at_ms: 1, action: exo_book_event}\n").contains("requires `book`"));
    }

    #[test]
    fn tracker_flow_note_is_required_and_single_line() {
        let e = err(
            "  - {at_ms: 1, action: tracker_flow, book: D1-CORE, instrument: AAPL, target_qty: 5, band_qty: 1}\n",
        );
        assert!(e.contains("`note`"), "{e}");
        let e = err(
            "  - {at_ms: 1, action: tracker_flow, book: D1-CORE, instrument: AAPL, target_qty: 5, band_qty: 1, note: \"a\\nb\"}\n",
        );
        assert!(e.contains("single-line"), "{e}");
    }

    #[test]
    fn tracker_flow_non_finite_quantities_error() {
        let e = err(
            "  - {at_ms: 1, action: tracker_flow, book: D1-CORE, instrument: AAPL, target_qty: 5, band_qty: .nan, note: n}\n",
        );
        assert!(e.contains("finite"), "{e}");
        let e = err(
            "  - {at_ms: 1, action: tracker_flow, book: D1-CORE, instrument: AAPL, target_qty: .inf, band_qty: 1, note: n}\n",
        );
        assert!(e.contains("finite"), "{e}");
    }
}
