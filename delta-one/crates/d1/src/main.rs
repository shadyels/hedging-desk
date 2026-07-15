//! Delta One process binary: hosts the core thread (`OrderStore` +
//! `PositionKeeper`) and starts the FIX gateway, wired together over `rtrb`
//! rings (ADR-013). docs/ROADMAP.md P1.M2 slice 2.
//!
//! ponytail: the startup order below is a CLI-driven stand-in for the
//! netting-driven emit that lands in P1.M3 -- there is no EXO target /
//! netting yet, so this binary just places one order from CLI flags on
//! startup and then drives whatever execs come back.

use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::Duration;

use anyhow::{Context, Result, bail};
use d1_core::{BookId, ClOrdId, ExecEvent, InstrumentId, Order, OrderStatus, OrderStore, Side};
use d1_gateway_fix::FixCallbacks;

/// Must match `crates/d1-gateway-fix/initiator.cfg`'s `[SESSION]` block.
const SENDER_COMP_ID: &str = "D1";
const TARGET_COMP_ID: &str = "SIM";
const DEFAULT_INITIATOR_CFG: &str = "crates/d1-gateway-fix/initiator.cfg";
const RING_CAPACITY: usize = 64;
const POLL_INTERVAL: Duration = Duration::from_millis(5);

struct StartupOrderArgs {
    book: BookId,
    instrument: InstrumentId,
    side: Side,
    qty_e2: i64,
    px_e9: i64,
    cfg: PathBuf,
}

fn main() -> Result<()> {
    let args = parse_args()?;

    let shutdown = Arc::new(AtomicBool::new(false));
    {
        let shutdown = Arc::clone(&shutdown);
        ctrlc::set_handler(move || shutdown.store(true, Ordering::Relaxed))
            .context("registering Ctrl-C handler")?;
    }

    let (outbound_tx, outbound_rx) = rtrb::RingBuffer::<Order>::new(RING_CAPACITY);
    let (inbound_tx, inbound_rx) = rtrb::RingBuffer::<ExecEvent>::new(RING_CAPACITY);

    let core_shutdown = Arc::clone(&shutdown);
    let core_handle = thread::spawn(move || {
        run_core(
            args.book,
            args.instrument,
            args.side,
            args.qty_e2,
            args.px_e9,
            outbound_tx,
            inbound_rx,
            &core_shutdown,
        );
    });

    let gateway_shutdown = Arc::clone(&shutdown);
    let callbacks = FixCallbacks::new(inbound_tx);
    let gateway_cfg = args.cfg;
    let gateway_handle = thread::spawn(move || {
        d1_gateway_fix::run_initiator(
            &gateway_cfg,
            SENDER_COMP_ID,
            TARGET_COMP_ID,
            &callbacks,
            outbound_rx,
            &gateway_shutdown,
        )
    });

    println!("d1: running (Ctrl-C to shut down)");
    while !shutdown.load(Ordering::Relaxed) {
        thread::sleep(POLL_INTERVAL);
    }
    println!("d1: shutting down");

    core_handle
        .join()
        .map_err(|_| anyhow::anyhow!("core thread panicked"))?;
    match gateway_handle.join() {
        Ok(result) => result.context("FIX gateway")?,
        Err(_) => bail!("FIX gateway thread panicked"),
    }

    Ok(())
}

/// Core thread: owns `OrderStore`, places the one startup order, then polls
/// the inbound-exec ring and drives `apply_exec` -> position keeper, exactly
/// the architecture diagram's "CORE THREAD" box. This is the M2 demo stand-in
/// for the hot path, not the benchmarked hot path itself (that's `d1-core`,
/// exercised by `crates/d1-core/benches/hot_path.rs`); the `println!`s here
/// are the manual-verification signal the DoD asks for, not something the
/// `just bench` latency budget covers.
#[allow(clippy::too_many_arguments)]
fn run_core(
    book: BookId,
    instrument: InstrumentId,
    side: Side,
    qty_e2: i64,
    px_e9: i64,
    mut outbound_tx: rtrb::Producer<Order>,
    mut inbound_rx: rtrb::Consumer<ExecEvent>,
    shutdown: &AtomicBool,
) {
    let mut store = OrderStore::new(RING_CAPACITY);
    let mut keeper = d1_core::PositionKeeper::new(&[book], &[instrument]);

    let cl_ord_id = ClOrdId::from_seq(1);
    let order = Order {
        cl_ord_id,
        book,
        instrument,
        side,
        order_qty_e2: qty_e2,
        limit_px_e9: px_e9,
        status: OrderStatus::New,
        cum_qty_e2: 0,
        leaves_qty_e2: 0,
        last_px_e9: 0,
    };
    store.place(order);

    let mut pending = Some(order);
    while let Some(next) = pending.take() {
        if shutdown.load(Ordering::Relaxed) {
            return;
        }
        match outbound_tx.push(next) {
            Ok(()) => {
                println!(
                    "d1: placed startup order book={book:?} instrument={instrument:?} side={side:?} qty_e2={qty_e2} px_e9={px_e9}"
                );
            }
            Err(rtrb::PushError::Full(returned)) => {
                pending = Some(returned);
                thread::sleep(POLL_INTERVAL);
            }
        }
    }

    while !shutdown.load(Ordering::Relaxed) {
        match inbound_rx.pop() {
            Ok(event) => match store.apply_exec(&event) {
                Ok(Some(fill)) => {
                    keeper.apply_fill(
                        fill.book,
                        fill.instrument,
                        fill.side,
                        fill.qty_e2,
                        fill.px_e9,
                    );
                    println!(
                        "d1: fill qty_e2={} px_e9={} book={:?} instrument={:?}",
                        fill.qty_e2, fill.px_e9, fill.book, fill.instrument
                    );
                }
                Ok(None) => {}
                Err(err) => eprintln!("d1: apply_exec error: {err}"),
            },
            Err(rtrb::PopError::Empty) => thread::sleep(POLL_INTERVAL),
        }
    }
}

fn parse_args() -> Result<StartupOrderArgs> {
    let mut book: Option<u32> = None;
    let mut instrument: Option<u32> = None;
    let mut side: Option<Side> = None;
    let mut qty_e2: Option<i64> = None;
    let mut px_e9: i64 = 0;
    let mut cfg = PathBuf::from(DEFAULT_INITIATOR_CFG);

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
            other => bail!("unknown argument '{other}'"),
        }
    }

    Ok(StartupOrderArgs {
        book: BookId(book.ok_or_else(|| anyhow::anyhow!("--book is required"))?),
        instrument: InstrumentId(
            instrument.ok_or_else(|| anyhow::anyhow!("--instrument is required"))?,
        ),
        side: side.ok_or_else(|| anyhow::anyhow!("--side is required"))?,
        qty_e2: qty_e2.ok_or_else(|| anyhow::anyhow!("--qty is required"))?,
        px_e9,
        cfg,
    })
}

fn next_arg(args: &mut impl Iterator<Item = String>, flag: &str) -> Result<String> {
    args.next()
        .ok_or_else(|| anyhow::anyhow!("{flag} requires a value"))
}
