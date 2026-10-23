//! Deterministic scenario replay: resolves symbols via refdata, builds
//! `MarketData` + `PositionKeeper`, seeds each tracker book's starting cash,
//! expands the timeline via `emit::expand` (the single scenario -> tick
//! reading, shared with `--mode emit-ticks`), applies each tick to
//! `MarketData::ingest`/`PositionKeeper::credit_dividend` in order, and
//! prints the resulting book plus per-book cash. The demoable M1 output (docs/ROADMAP.md P1.M1), extended for per-book cash + dividends
//! in P1.M5 Slice 1.
//!
//! `quote`/`gap`/`dividend` semantics (fixed-point conversion at the boundary,
//! `gap` as bid=ask=last, the ex-div price drop) live in `emit::expand`; a
//! dividend tick drops the stored price AND credits cash here, matching what
//! `d1::run_core` does with the same tick.

use std::path::Path;

use anyhow::{Context, Result, bail};
use d1_core::{FeedTick, InstrumentId, MarketData, PositionKeeper};
use d1_refdata::Universe;

use crate::scenario;

pub(crate) fn to_fixed_e9(px: f64) -> i64 {
    (px * 1_000_000_000.0).round() as i64
}

/// Run a scenario file end to end and print the resulting market-data book.
pub fn run(scenario_path: &Path) -> Result<()> {
    let scenario = scenario::load(scenario_path)
        .with_context(|| format!("loading scenario {}", scenario_path.display()))?;

    let scenario_dir = scenario_path.parent().unwrap_or_else(|| Path::new("."));
    let universe_path = scenario_dir.join(&scenario.universe);
    let universe = d1_refdata::load(&universe_path)?;

    let mut market_data = MarketData::new(&universe.instrument_ids);
    // Not driven by fills: each tracker book's `initial_cash_e9` is seeded
    // below and dividend ticks credit it (P1.M5 Slice 1).
    let mut keeper = PositionKeeper::new(&universe.book_ids, &universe.instrument_ids);
    for tracker in &universe.tracker_books {
        if keeper
            .seed_cash(tracker.book, tracker.initial_cash_e9)
            .is_none()
        {
            bail!(
                "seeding cash for tracker book {:?}: book not in this universe's keeper",
                tracker.book
            );
        }
    }

    println!(
        "sim: replaying '{}' (seed={})",
        scenario.scenario, scenario.seed
    );

    let (ticks, _skipped) = crate::emit::expand(&scenario, &universe)?;
    apply_ticks(&ticks, &mut market_data, &mut keeper)?;

    print_book(&universe, &market_data, &keeper);
    Ok(())
}

/// Apply expanded ticks in order: price into `market_data`, and any dividend
/// carried on the tick into every holding book's cash. Credits every book
/// holding the instrument, not just tracker books -- same all-or-nothing
/// overflow discipline as any other keeper mutation (root CLAUDE.md #2): a
/// `None` must not pass silently.
fn apply_ticks(
    ticks: &[FeedTick],
    market_data: &mut MarketData,
    keeper: &mut PositionKeeper,
) -> Result<()> {
    for tick in ticks {
        market_data.ingest(tick);
        if tick.div_per_share_e9 != 0
            && keeper
                .credit_dividend(tick.instrument_id, tick.div_per_share_e9)
                .is_none()
        {
            bail!(
                "dividend at {}ns instrument={:?} div_per_share_e9={}: credit_dividend overflowed, cash NOT applied",
                tick.exch_ts_ns,
                tick.instrument_id,
                tick.div_per_share_e9
            );
        }
    }
    Ok(())
}

pub(crate) fn resolve(universe: &Universe, symbol: &str, at_ms: u64) -> Result<InstrumentId> {
    universe
        .symbol_to_id
        .get(symbol)
        .copied()
        .with_context(|| format!("unknown instrument symbol '{symbol}' at {at_ms}ms"))
}

fn print_book(universe: &Universe, market_data: &MarketData, keeper: &PositionKeeper) {
    let id_to_symbol: std::collections::HashMap<_, _> = universe
        .symbol_to_id
        .iter()
        .map(|(symbol, id)| (*id, symbol.as_str()))
        .collect();

    println!(
        "{:<10} {:>16} {:>16} {:>16}",
        "symbol", "bid_e9", "ask_e9", "last_e9"
    );
    for (id, quote) in market_data.iter() {
        if quote.exch_ts_ns == 0 {
            continue; // never ticked this run
        }
        let symbol = id_to_symbol.get(&id).copied().unwrap_or("?");
        println!(
            "{:<10} {:>16} {:>16} {:>16}",
            symbol, quote.bid_px_e9, quote.ask_px_e9, quote.last_px_e9
        );
    }

    if !universe.tracker_books.is_empty() {
        println!("{:<10} {:>20}", "book", "cash_e9");
        for tracker in &universe.tracker_books {
            let cash = keeper.cash(tracker.book).unwrap_or(0);
            println!("{:<10} {:>20}", format!("{:?}", tracker.book), cash);
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)] // tests: unwrap_used is a hot-path-only ban (delta-one/CLAUDE.md)
mod tests {
    use super::*;
    use d1_core::{BookId, Side};

    #[test]
    fn dividend_tick_drops_stored_price_and_credits_cash() {
        let (book, inst) = (BookId(1), InstrumentId(1001));
        let mut md = MarketData::new(&[inst]);
        let mut keeper = PositionKeeper::new(&[book], &[inst]);
        keeper.seed_cash(book, 0).unwrap();
        keeper
            .apply_fill(book, inst, Side::Buy, 10_000, 0) // 100.00 shares at px 0: cash stays 0
            .unwrap();
        let px = |p: i64, div: i64, ts: u64| FeedTick {
            instrument_id: inst,
            bid_px_e9: p,
            ask_px_e9: p,
            last_px_e9: p,
            exch_ts_ns: ts,
            div_per_share_e9: div,
        };
        let ticks = [
            px(176_000_000_000, 0, 1),
            px(175_750_000_000, 250_000_000, 2),
        ];
        apply_ticks(&ticks, &mut md, &mut keeper).unwrap();
        assert_eq!(md.quote(inst).unwrap().last_px_e9, 175_750_000_000);
        // 100 shares * 0.25 = 25.00 -> 25e9
        assert_eq!(keeper.cash(book), Some(25_000_000_000));
    }
}
