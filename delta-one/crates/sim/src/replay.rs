//! Deterministic scenario replay: resolves symbols via refdata, builds
//! `MarketData` + `PositionKeeper`, seeds each tracker book's starting cash,
//! feeds the timeline into `MarketData::ingest`/`PositionKeeper::credit_dividend`
//! in order, and prints the resulting book plus per-book cash. The demoable
//! M1 output (docs/ROADMAP.md P1.M1), extended for per-book cash + dividends
//! in P1.M5 Slice 1.
//!
//! ponytail: `quote`/`gap` synthesize bid/ask/last directly from the
//! scenario's decimal values (fixed-point conversion at this boundary, per
//! root CLAUDE.md invariant 1). `gap` has no spread info in the scenario
//! schema, so bid=ask=last=to_mid — a real feed replay would preserve the
//! last known spread; add that when a scenario needs it.

use std::path::Path;

use anyhow::{Context, Result, bail};
use d1_core::{FeedTick, InstrumentId, MarketData, PositionKeeper};
use d1_refdata::Universe;

use crate::scenario;

fn to_fixed_e9(px: f64) -> i64 {
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
    // Not driven by fills yet -- those arrive with the M2 order path -- but
    // now real for cash: each tracker book's `initial_cash_e9` is seeded
    // below and the `dividend` timeline arm credits it (P1.M5 Slice 1).
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

    for entry in &scenario.timeline {
        let Some(action) = entry.action.as_deref() else {
            continue;
        };
        match action {
            "quote" => {
                let (Some(symbol), Some(bid), Some(ask)) =
                    (&entry.instrument, entry.bid, entry.ask)
                else {
                    bail!("quote at {}ms missing instrument/bid/ask", entry.at_ms);
                };
                let id = resolve(&universe, symbol, entry.at_ms)?;
                let bid_px_e9 = to_fixed_e9(bid);
                let ask_px_e9 = to_fixed_e9(ask);
                market_data.ingest(&FeedTick {
                    instrument_id: id,
                    bid_px_e9,
                    ask_px_e9,
                    last_px_e9: (bid_px_e9 + ask_px_e9) / 2,
                    exch_ts_ns: entry.at_ms * 1_000_000,
                    div_per_share_e9: 0,
                });
            }
            "gap" => {
                let (Some(symbol), Some(to_mid)) = (&entry.instrument, entry.to_mid) else {
                    bail!("gap at {}ms missing instrument/to_mid", entry.at_ms);
                };
                let id = resolve(&universe, symbol, entry.at_ms)?;
                let px_e9 = to_fixed_e9(to_mid);
                market_data.ingest(&FeedTick {
                    instrument_id: id,
                    bid_px_e9: px_e9,
                    ask_px_e9: px_e9,
                    last_px_e9: px_e9,
                    exch_ts_ns: entry.at_ms * 1_000_000,
                    div_per_share_e9: 0,
                });
            }
            "dividend" => {
                let (Some(symbol), Some(amount)) = (&entry.instrument, entry.amount_per_share)
                else {
                    bail!(
                        "dividend at {}ms missing instrument/amount_per_share",
                        entry.at_ms
                    );
                };
                let id = resolve(&universe, symbol, entry.at_ms)?;
                let div_per_share_e9 = to_fixed_e9(amount);
                // Credits every book holding `id`, not just tracker books --
                // same all-or-nothing overflow discipline as any other
                // keeper mutation (root CLAUDE.md #2): a `None` here must
                // not pass silently.
                if keeper.credit_dividend(id, div_per_share_e9).is_none() {
                    bail!(
                        "dividend at {}ms instrument={:?} amount_per_share={amount}: credit_dividend overflowed, cash NOT applied",
                        entry.at_ms,
                        id
                    );
                }
            }
            // exo_book_event / anything else: no EXO book or order path in
            // M1 — nothing to do yet.
            _ => {}
        }
    }

    print_book(&universe, &market_data, &keeper);
    Ok(())
}

fn resolve(universe: &Universe, symbol: &str, at_ms: u64) -> Result<InstrumentId> {
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
