//! Delta One process binary: parses CLI flags and hands off to `d1::spawn`
//! (`src/lib.rs`), which owns the actual ring/thread wiring (ADR-013) --
//! shared with `tests/nats_round_trip.rs` so both exercise the real setup.
//! docs/ROADMAP.md P1.M2 slice 3.
//!
//! ponytail: the startup order below is a CLI-driven stand-in for the
//! netting-driven emit that lands in P1.M3 -- there is no real netting yet,
//! so this binary places one order from CLI flags on startup in addition to
//! whatever `TargetPosition`s arrive over NATS.

use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::Duration;

use anyhow::{Context, Result, bail};
use d1::{FixConfig, StartupOrder, spawn};
use d1_core::{BookId, InstrumentId, Side};

/// Must match `crates/d1-gateway-fix/initiator.cfg`'s `[SESSION]` block.
const SENDER_COMP_ID: &str = "D1";
const TARGET_COMP_ID: &str = "SIM";
const DEFAULT_INITIATOR_CFG: &str = "crates/d1-gateway-fix/initiator.cfg";
/// NATS client port (`deploy/docker-compose.yml`).
const DEFAULT_NATS_URL: &str = "127.0.0.1:4222";
/// Relative to `delta-one/`, this binary's cwd (`justfile`'s `cd delta-one && cargo run -p d1`).
const DEFAULT_UNIVERSE: &str = "../protocol/refdata/universe.json";
const MAIN_POLL_INTERVAL: Duration = Duration::from_millis(5);

struct Args {
    startup: StartupOrder,
    cfg: PathBuf,
    nats_url: String,
    universe: PathBuf,
}

fn main() -> Result<()> {
    let args = parse_args()?;

    let universe = d1_refdata::load(&args.universe).context("loading universe refdata")?;
    // ADR-005 §4: the cross reference-price policy is compliance-visible and
    // must never be a silent default. Parse it at startup so a typo in refdata
    // kills the process here rather than mispricing internal risk transfers.
    // ponytail: validated-only this slice — the netting engine is unwired
    // (target_to_order still stands). Slice 2 threads the parsed policy into
    // the netting cycle; today parsing it IS the gate.
    let _policy: d1_netting::RefPxPolicy = universe.cross_px_policy.parse().with_context(|| {
        format!(
            "unknown cross_px_policy in refdata: {:?}",
            universe.cross_px_policy
        )
    })?;

    let shutdown = Arc::new(AtomicBool::new(false));
    {
        let shutdown = Arc::clone(&shutdown);
        ctrlc::set_handler(move || shutdown.store(true, Ordering::Relaxed))
            .context("registering Ctrl-C handler")?;
    }

    let handles = spawn(
        args.startup,
        FixConfig {
            settings_path: args.cfg,
            sender_comp_id: SENDER_COMP_ID.to_string(),
            target_comp_id: TARGET_COMP_ID.to_string(),
        },
        args.nats_url,
        universe.book_ids,
        universe.instrument_ids,
        &shutdown,
    );

    println!("d1: running (Ctrl-C to shut down)");
    while !shutdown.load(Ordering::Relaxed) {
        thread::sleep(MAIN_POLL_INTERVAL);
    }
    println!("d1: shutting down");

    handles
        .core
        .join()
        .map_err(|_| anyhow::anyhow!("core thread panicked"))?;
    handles
        .feed
        .join()
        .map_err(|_| anyhow::anyhow!("feed thread panicked"))?;
    match handles.fix.join() {
        Ok(result) => result.context("FIX gateway")?,
        Err(_) => bail!("FIX gateway thread panicked"),
    }
    match handles.nats.join() {
        // ponytail: NATS gateway errors are logged, not fatal -- degraded
        // mode (DoD #4: `d1` stays up FIX-only when NATS is unreachable).
        Ok(Ok(())) => {}
        Ok(Err(err)) => eprintln!("d1: NATS gateway exited with error: {err}"),
        Err(_) => eprintln!("d1: NATS gateway thread panicked"),
    }

    Ok(())
}

fn parse_args() -> Result<Args> {
    let mut book: Option<u32> = None;
    let mut instrument: Option<u32> = None;
    let mut side: Option<Side> = None;
    let mut qty_e2: Option<i64> = None;
    let mut px_e9: i64 = 0;
    let mut cfg = PathBuf::from(DEFAULT_INITIATOR_CFG);
    let mut nats_url = DEFAULT_NATS_URL.to_string();
    let mut universe = PathBuf::from(DEFAULT_UNIVERSE);

    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--book" => {
                book = Some(
                    next_arg(&mut args, "--book")?
                        .parse()
                        .context("--book must be a u32")?,
                );
            }
            "--instrument" => {
                instrument = Some(
                    next_arg(&mut args, "--instrument")?
                        .parse()
                        .context("--instrument must be a u32")?,
                );
            }
            "--side" => {
                side = Some(match next_arg(&mut args, "--side")?.as_str() {
                    "buy" => Side::Buy,
                    "sell" => Side::Sell,
                    other => bail!("--side must be buy|sell, got '{other}'"),
                });
            }
            "--qty" => {
                qty_e2 = Some(
                    next_arg(&mut args, "--qty")?
                        .parse()
                        .context("--qty must be an integer, fixed-point x10^2")?,
                );
            }
            "--px" => {
                px_e9 = next_arg(&mut args, "--px")?
                    .parse()
                    .context("--px must be an integer, fixed-point x10^9 (0 = market)")?;
            }
            "--cfg" => cfg = PathBuf::from(next_arg(&mut args, "--cfg")?),
            "--nats-url" => nats_url = next_arg(&mut args, "--nats-url")?,
            "--universe" => universe = PathBuf::from(next_arg(&mut args, "--universe")?),
            other => bail!("unknown argument '{other}'"),
        }
    }

    Ok(Args {
        startup: StartupOrder {
            book: BookId(book.ok_or_else(|| anyhow::anyhow!("--book is required"))?),
            instrument: InstrumentId(
                instrument.ok_or_else(|| anyhow::anyhow!("--instrument is required"))?,
            ),
            side: side.ok_or_else(|| anyhow::anyhow!("--side is required"))?,
            qty_e2: qty_e2.ok_or_else(|| anyhow::anyhow!("--qty is required"))?,
            px_e9,
        },
        cfg,
        nats_url,
        universe,
    })
}

fn next_arg(args: &mut impl Iterator<Item = String>, flag: &str) -> Result<String> {
    args.next()
        .ok_or_else(|| anyhow::anyhow!("{flag} requires a value"))
}
