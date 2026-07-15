//! Delta One core-thread wiring, shared by the `d1` binary (`main.rs`) and
//! its integration tests (`tests/fix_round_trip.rs`, `tests/nats_round_trip.rs`)
//! so both exercise the real ring/thread setup instead of a hand-duplicated
//! copy. `docs/ROADMAP.md` P1.M2 slice 3: the feed-ingest ring/thread
//! (deferred from Slice 2) plus the NATS target/exec-report rings.

pub mod feed;

use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use d1_core::{
    BookId, ClOrdId, ExecEvent, ExecReport, FeedTick, InstrumentId, MarketData, Order, OrderStatus,
    OrderStore, PositionKeeper, Side, Target, target_to_order,
};
use d1_gateway_fix::{FixCallbacks, FixError};
use d1_gateway_nats::NatsError;

/// Ring capacity for every `rtrb` ring this binary owns (ADR-013). Generous
/// for a demo-sized single session, matching `d1-gateway-fix`'s ring sizing.
pub const RING_CAPACITY: usize = 64;
/// Poll/backoff interval for the core thread's drain loop.
pub const POLL_INTERVAL: Duration = Duration::from_millis(5);

/// The CLI-driven startup order (Slice 2 stand-in for the netting-driven
/// emit that lands in P1.M3): placed once at core-thread startup, on top of
/// whatever `TargetPosition`s arrive over NATS afterward.
#[derive(Debug, Clone, Copy)]
pub struct StartupOrder {
    /// Book the startup order books to.
    pub book: BookId,
    /// Instrument to trade.
    pub instrument: InstrumentId,
    /// Buy or sell.
    pub side: Side,
    /// Requested quantity, fixed-point x10^2.
    pub qty_e2: i64,
    /// Limit price, fixed-point x10^9 (0 = market).
    pub px_e9: i64,
}

/// FIX session identity + config, passed straight to
/// `d1_gateway_fix::run_initiator`.
#[derive(Debug, Clone)]
pub struct FixConfig {
    /// Path to the `quickfix` session settings file.
    pub settings_path: PathBuf,
    /// This session's `SenderCompID`.
    pub sender_comp_id: String,
    /// This session's `TargetCompID`.
    pub target_comp_id: String,
}

/// The spawned thread handles `spawn` returns. The caller (the `d1` binary,
/// or an integration test) owns shutdown-triggering and joining -- `spawn`
/// itself blocks on nothing.
pub struct RunHandles {
    /// The core thread (`OrderStore` + `PositionKeeper` + `MarketData`).
    pub core: JoinHandle<()>,
    /// The FIX initiator thread.
    pub fix: JoinHandle<Result<(), FixError>>,
    /// The NATS gateway thread.
    pub nats: JoinHandle<Result<(), NatsError>>,
    /// The synthetic feed-ingest producer thread.
    pub feed: JoinHandle<()>,
}

/// Build the `rtrb` rings (ADR-013) and spawn the core/FIX/NATS/feed
/// threads. Blocks on nothing itself -- the caller decides how/when to flip
/// `shutdown` and joins the returned handles.
#[must_use]
pub fn spawn(
    startup: StartupOrder,
    fix_cfg: FixConfig,
    nats_url: String,
    shutdown: &Arc<AtomicBool>,
) -> RunHandles {
    let (fix_outbound_tx, fix_outbound_rx) = rtrb::RingBuffer::<Order>::new(RING_CAPACITY);
    let (fix_inbound_tx, fix_inbound_rx) = rtrb::RingBuffer::<ExecEvent>::new(RING_CAPACITY);
    let (target_tx, target_rx) = rtrb::RingBuffer::<Target>::new(RING_CAPACITY);
    let (exec_report_tx, exec_report_rx) = rtrb::RingBuffer::<ExecReport>::new(RING_CAPACITY);
    let (feed_tx, feed_rx) = rtrb::RingBuffer::<FeedTick>::new(RING_CAPACITY);

    let core_shutdown = Arc::clone(shutdown);
    let core = thread::spawn(move || {
        run_core(
            startup,
            fix_outbound_tx,
            fix_inbound_rx,
            target_rx,
            exec_report_tx,
            feed_rx,
            &core_shutdown,
        );
    });

    let fix_shutdown = Arc::clone(shutdown);
    let callbacks = FixCallbacks::new(fix_inbound_tx);
    let fix = thread::spawn(move || {
        d1_gateway_fix::run_initiator(
            &fix_cfg.settings_path,
            &fix_cfg.sender_comp_id,
            &fix_cfg.target_comp_id,
            &callbacks,
            fix_outbound_rx,
            &fix_shutdown,
        )
    });

    let nats_shutdown = Arc::clone(shutdown);
    let nats = thread::spawn(move || {
        d1_gateway_nats::run_gateway(&nats_url, target_tx, exec_report_rx, &nats_shutdown)
    });

    let feed_shutdown = Arc::clone(shutdown);
    let feed =
        thread::spawn(move || feed::run_feed_producer(startup.instrument, feed_tx, &feed_shutdown));

    RunHandles {
        core,
        fix,
        nats,
        feed,
    }
}

/// Core thread: places the CLI-driven startup order, then each poll drains
/// (in order) the feed ring -> `MarketData::ingest`, the target ring ->
/// `target_to_order` -> `OrderStore::place` + FIX outbound, and the FIX
/// inbound-exec ring -> `apply_exec` -> `ExecReport` (NATS outbound) +
/// `PositionKeeper::apply_fill`. Manual-verification `println!`s only, same
/// as Slice 2 -- not the benchmarked hot path
/// (`d1-core/benches/hot_path.rs` covers that).
#[allow(clippy::too_many_arguments)]
fn run_core(
    startup: StartupOrder,
    mut fix_outbound_tx: rtrb::Producer<Order>,
    mut fix_inbound_rx: rtrb::Consumer<ExecEvent>,
    mut target_rx: rtrb::Consumer<Target>,
    mut exec_report_tx: rtrb::Producer<ExecReport>,
    mut feed_rx: rtrb::Consumer<FeedTick>,
    shutdown: &AtomicBool,
) {
    let mut store = OrderStore::new(RING_CAPACITY);
    let mut keeper = PositionKeeper::new(&[startup.book], &[startup.instrument]);
    let mut market_data = MarketData::new(&[startup.instrument]);
    let mut next_seq = 2u64; // seq 1 is the startup order below

    let cl_ord_id = ClOrdId::from_seq(1);
    let order = Order {
        cl_ord_id,
        book: startup.book,
        instrument: startup.instrument,
        side: startup.side,
        order_qty_e2: startup.qty_e2,
        limit_px_e9: startup.px_e9,
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
        match fix_outbound_tx.push(next) {
            Ok(()) => println!(
                "d1: placed startup order book={:?} instrument={:?} side={:?} qty_e2={} px_e9={}",
                startup.book, startup.instrument, startup.side, startup.qty_e2, startup.px_e9
            ),
            Err(rtrb::PushError::Full(returned)) => {
                pending = Some(returned);
                thread::sleep(POLL_INTERVAL);
            }
        }
    }

    while !shutdown.load(Ordering::Relaxed) {
        let mut did_work = false;

        if let Ok(tick) = feed_rx.pop() {
            did_work = true;
            if market_data.ingest(&tick) {
                println!(
                    "d1: quote instrument={:?} bid={} ask={} last={}",
                    tick.instrument_id, tick.bid_px_e9, tick.ask_px_e9, tick.last_px_e9
                );
            }
        }

        if let Ok(target) = target_rx.pop() {
            did_work = true;
            if let Some(order) = target_to_order(&target, ClOrdId::from_seq(next_seq)) {
                next_seq += 1;
                store.place(order);
                println!(
                    "d1: target-driven order book={:?} instrument={:?} side={:?} qty_e2={}",
                    order.book, order.instrument, order.side, order.order_qty_e2
                );
                if fix_outbound_tx.push(order).is_err() {
                    eprintln!("d1: FIX outbound ring full, dropping target-driven order");
                }
            }
        }

        if let Ok(event) = fix_inbound_rx.pop() {
            did_work = true;
            match store.apply_exec(&event) {
                Ok(fill) => {
                    if let Some(fill) = fill {
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
                    // ponytail: log-and-drop on a full ring, same ceiling as
                    // every other ring in this binary -- a single demo
                    // session, not a backpressure protocol yet.
                    if let Some(order) = store.get(event.cl_ord_id) {
                        let report = ExecReport {
                            cl_ord_id: event.cl_ord_id,
                            exec_id: event.exec_id,
                            book: order.book,
                            instrument: order.instrument,
                            side: order.side,
                            status: order.status,
                            last_qty_e2: event.last_qty_e2,
                            last_px_e9: event.last_px_e9,
                            cum_qty_e2: order.cum_qty_e2,
                            leaves_qty_e2: order.leaves_qty_e2,
                        };
                        if exec_report_tx.push(report).is_err() {
                            eprintln!("d1: NATS outbound ring full, dropping ExecutionReport");
                        }
                    }
                }
                Err(err) => eprintln!("d1: apply_exec error: {err}"),
            }
        }

        if !did_work {
            thread::sleep(POLL_INTERVAL);
        }
    }
}
