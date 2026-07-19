//! Delta One core-thread wiring, shared by the `d1` binary (`main.rs`) and
//! its integration tests (`tests/fix_round_trip.rs`, `tests/nats_round_trip.rs`)
//! so both exercise the real ring/thread setup instead of a hand-duplicated
//! copy. `docs/ROADMAP.md` P1.M2 slice 3: the feed-ingest ring/thread
//! (deferred from Slice 2) plus the NATS target/exec-report rings.

pub mod cycle;
pub mod feed;

use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use cycle::{NettingSession, allocate_fill};
use d1_core::{
    BookId, ClOrdId, CrossRecord, ExecEvent, ExecReport, FeedTick, InstrumentId, MarketData, Order,
    OrderStatus, OrderStore, PositionKeeper, Side, Target, TransferRequest,
};
use d1_gateway_fix::{FixCallbacks, FixError};
use d1_gateway_nats::NatsError;
use d1_netting::RefPxPolicy;

/// Ring capacity for every `rtrb` ring this binary owns (ADR-013). Generous
/// for a demo-sized single session, matching `d1-gateway-fix`'s ring sizing.
pub const RING_CAPACITY: usize = 64;
/// Poll/backoff interval for the core thread's drain loop.
pub const POLL_INTERVAL: Duration = Duration::from_millis(5);

/// The CLI-driven startup order: placed once at core-thread startup as a
/// position + FIX round-trip anchor, additive alongside whatever
/// `TargetPosition`s arrive over NATS and get netted through
/// `cycle::NettingSession` afterward.
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
///
/// `book_ids`/`instrument_ids` are the keeper/market-data universe (P1.M3
/// slice 1: loaded from `protocol/refdata/universe.json` via `d1-refdata` by
/// the caller). `startup.book`/`startup.instrument` must be included in
/// these lists for the startup order to book anywhere -- the wildcard-target
/// guard in `run_core` already handles a startup pair that isn't configured,
/// so `spawn` does not re-validate that here.
#[must_use]
pub fn spawn(
    startup: StartupOrder,
    fix_cfg: FixConfig,
    nats_url: String,
    book_ids: Vec<BookId>,
    instrument_ids: Vec<InstrumentId>,
    policy: RefPxPolicy,
    shutdown: &Arc<AtomicBool>,
) -> RunHandles {
    let (fix_outbound_tx, fix_outbound_rx) = rtrb::RingBuffer::<Order>::new(RING_CAPACITY);
    let (fix_inbound_tx, fix_inbound_rx) = rtrb::RingBuffer::<ExecEvent>::new(RING_CAPACITY);
    let (target_tx, target_rx) = rtrb::RingBuffer::<Target>::new(RING_CAPACITY);
    let (exec_report_tx, exec_report_rx) = rtrb::RingBuffer::<ExecReport>::new(RING_CAPACITY);
    let (feed_tx, feed_rx) = rtrb::RingBuffer::<FeedTick>::new(RING_CAPACITY);
    let (cross_tx, cross_rx) = rtrb::RingBuffer::<CrossRecord>::new(RING_CAPACITY);
    let (transfer_tx, transfer_rx) = rtrb::RingBuffer::<TransferRequest>::new(RING_CAPACITY);

    let core_shutdown = Arc::clone(shutdown);
    let core = thread::spawn(move || {
        run_core(
            startup,
            fix_outbound_tx,
            fix_inbound_rx,
            target_rx,
            exec_report_tx,
            feed_rx,
            cross_tx,
            transfer_rx,
            book_ids,
            instrument_ids,
            policy,
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
        d1_gateway_nats::run_gateway(
            &nats_url,
            target_tx,
            exec_report_rx,
            cross_rx,
            transfer_tx,
            &nats_shutdown,
        )
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
/// `cycle::NettingSession::on_target` -> internal cross legs booked (each
/// pushed to `cross_tx` as a `CrossRecord` for NATS `d1.crosses` publish,
/// Slice 3) and `OrderStore::place` plus FIX outbound for the resulting
/// parent order, the transfer ring -> universe/sanity validation ->
/// `cycle::NettingSession::on_transfer` -> one more `CrossRecord` pushed to
/// `cross_tx` (Slice 3, ADR-009: no external order, no parent tracking), and
/// the FIX inbound-exec ring -> `apply_exec` -> `ExecReport` (NATS outbound)
/// and fill booking: pro-rata `cycle::allocate_fill` for netting parent
/// orders, direct `PositionKeeper::apply_fill` for the single-book startup
/// order. Manual-verification `println!`s only, same as Slice 2 -- not the
/// benchmarked hot path (`d1-core/benches/hot_path.rs` covers that).
///
/// `book_ids`/`instrument_ids` are the keeper/market-data universe (P1.M3
/// slice 1): every (book, instrument) pair drawn from these lists gets a
/// keeper slot, so the wildcard-target guard below now rejects only pairs
/// genuinely outside `protocol/refdata/universe.json`. `policy` is the
/// cross reference-price policy (ADR-005 §4), parsed and validated by the
/// caller at startup.
#[allow(clippy::too_many_arguments)]
fn run_core(
    startup: StartupOrder,
    mut fix_outbound_tx: rtrb::Producer<Order>,
    mut fix_inbound_rx: rtrb::Consumer<ExecEvent>,
    mut target_rx: rtrb::Consumer<Target>,
    mut exec_report_tx: rtrb::Producer<ExecReport>,
    mut feed_rx: rtrb::Consumer<FeedTick>,
    mut cross_tx: rtrb::Producer<CrossRecord>,
    mut transfer_rx: rtrb::Consumer<TransferRequest>,
    book_ids: Vec<BookId>,
    instrument_ids: Vec<InstrumentId>,
    policy: RefPxPolicy,
    shutdown: &AtomicBool,
) {
    let mut store = OrderStore::new(RING_CAPACITY);
    let mut keeper = PositionKeeper::new(&book_ids, &instrument_ids);
    let mut market_data = MarketData::new(&instrument_ids);
    let mut session = NettingSession::new(policy, 2); // seq 1 is the startup order below

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
            // The keeper's universe is the gate: `exo.targets.>` is a
            // wildcard, so a target can name a (book, instrument) this
            // process was never configured for. Placing that order would
            // fill at the venue and then have nowhere to book -- a position
            // lost in silence (root CLAUDE.md #2). Reject it instead.
            match keeper.position(target.book, target.instrument) {
                None => eprintln!(
                    "d1: target for unconfigured book={:?} instrument={:?}, rejecting (not in this process's position universe)",
                    target.book, target.instrument
                ),
                Some(_) => {
                    let ref_px_e9 = arrival_mid_px_e9(&market_data, target.instrument);
                    match session.on_target(target, &mut keeper, ref_px_e9) {
                        Ok(output) => {
                            if !output.crosses_to_book.is_empty() {
                                println!(
                                    "d1: booked {} internal cross leg(s) instrument={:?}",
                                    output.crosses_to_book.len(),
                                    target.instrument
                                );
                            }
                            // ponytail: log-and-drop on a full ring, same
                            // ceiling as every other ring in this binary --
                            // a single demo session, not a backpressure
                            // protocol yet.
                            for record in &output.crosses_to_book {
                                if cross_tx.push(*record).is_err() {
                                    eprintln!(
                                        "d1: cross ring full, dropping InternalCrossNotice cross_id={}",
                                        record.cross_id
                                    );
                                }
                            }
                            if let Some(order) = output.parent_order {
                                // Push to the wire BEFORE recording the
                                // order in `store`: a `place` that outlives
                                // a failed push leaves a phantom `New` order
                                // the venue never saw. `session.on_target`
                                // already registered this order's parent
                                // (weights/inflight) before returning it --
                                // ponytail: a dropped push here leaves that
                                // registration orphaned (never filled, its
                                // weight permanently `inflight`), same
                                // ring-full ceiling every other ring in this
                                // binary already accepts for a single demo
                                // session, not a backpressure protocol.
                                if fix_outbound_tx.push(order).is_err() {
                                    eprintln!(
                                        "d1: FIX outbound ring full, dropping netting parent order"
                                    );
                                } else {
                                    store.place(order);
                                    println!(
                                        "d1: netting parent order cl_ord_id={:?} instrument={:?} side={:?} qty_e2={}",
                                        order.cl_ord_id,
                                        order.instrument,
                                        order.side,
                                        order.order_qty_e2
                                    );
                                }
                            }
                        }
                        Err(err) => eprintln!("d1: netting cycle error: {err}"),
                    }
                }
            }
        }

        if let Ok(transfer) = transfer_rx.pop() {
            did_work = true;
            // Same universe gate as the target branch above (root CLAUDE.md
            // #2): a transfer naming a book/instrument this process has no
            // keeper slot for is rejected outright, plus the transfer-only
            // invariants (distinct books, positive qty) `d1_netting::net`
            // would otherwise enforce for netting-derived crosses.
            if transfer.from_book == transfer.to_book {
                eprintln!(
                    "d1: transfer rejected, from_book == to_book book={:?} instrument={:?}",
                    transfer.from_book, transfer.instrument
                );
            } else if transfer.qty_e2 <= 0 {
                // ponytail: no numeric upper bound on qty_e2 here -- a
                // business max-transfer-size limit is a Tier-1 risk check
                // (ADR-008, `d1.toml` config), not a code constant, and
                // lands with M4 risk limits. Overflow is still guarded by
                // `PositionKeeper::apply_cross`'s `checked_*` arithmetic
                // rejecting the cross outright (below), not by bounding the
                // input here.
                eprintln!(
                    "d1: transfer rejected, qty_e2 must be > 0, got {} instrument={:?}",
                    transfer.qty_e2, transfer.instrument
                );
            } else if keeper
                .position(transfer.from_book, transfer.instrument)
                .is_none()
                || keeper
                    .position(transfer.to_book, transfer.instrument)
                    .is_none()
            {
                eprintln!(
                    "d1: transfer for unconfigured book/instrument from_book={:?} to_book={:?} instrument={:?}, rejecting (not in this process's position universe)",
                    transfer.from_book, transfer.to_book, transfer.instrument
                );
            } else {
                let ref_px_e9 = arrival_mid_px_e9(&market_data, transfer.instrument);
                if let Some(record) = session.on_transfer(transfer, &mut keeper, ref_px_e9) {
                    println!(
                        "d1: booked directed transfer cross_id={} instrument={:?} buy_book={:?} sell_book={:?} qty_e2={}",
                        record.cross_id,
                        record.instrument,
                        record.buy_book,
                        record.sell_book,
                        record.qty_e2
                    );
                    if cross_tx.push(record).is_err() {
                        eprintln!(
                            "d1: cross ring full, dropping InternalCrossNotice cross_id={}",
                            record.cross_id
                        );
                    }
                } else {
                    eprintln!(
                        "d1: transfer not booked (overflow) instrument={:?} from_book={:?} to_book={:?} qty_e2={} -- not published",
                        transfer.instrument, transfer.from_book, transfer.to_book, transfer.qty_e2
                    );
                }
            }
        }

        if let Ok(event) = fix_inbound_rx.pop() {
            did_work = true;
            match store.apply_exec(&event) {
                Ok(fill) => {
                    if let Some(fill) = fill {
                        // A parent order's `Fill.book == BookId(0)` (the
                        // reserved firm-level pre-allocation, `live.proto`
                        // `ExecutionReport.book_id` doc comment) -- real
                        // attribution lives in the parent's weights, so a
                        // tracked parent routes through pro-rata
                        // allocation; anything else (the single-book
                        // startup order) books directly as before.
                        if let Some(parent) = session.parent_mut(event.cl_ord_id) {
                            let side = parent.side;
                            let instrument = parent.instrument;
                            for (book, qty_e2) in allocate_fill(parent, fill.qty_e2) {
                                if qty_e2 == 0 {
                                    continue;
                                }
                                // A `None` here means the allocation was
                                // never booked (unknown book/instrument, or
                                // a cost-basis overflow). The position is
                                // real either way, so this can never pass
                                // quietly (root CLAUDE.md #2).
                                if keeper
                                    .apply_fill(book, instrument, side, qty_e2, fill.px_e9)
                                    .is_none()
                                {
                                    eprintln!(
                                        "d1: FILL NOT BOOKED (parent allocation) book={book:?} instrument={instrument:?} side={side:?} qty_e2={qty_e2} px_e9={} -- firm position is now understated",
                                        fill.px_e9
                                    );
                                } else {
                                    println!(
                                        "d1: parent fill allocated book={book:?} instrument={instrument:?} qty_e2={qty_e2} px_e9={}",
                                        fill.px_e9
                                    );
                                }
                            }
                        } else if keeper
                            .apply_fill(
                                fill.book,
                                fill.instrument,
                                fill.side,
                                fill.qty_e2,
                                fill.px_e9,
                            )
                            .is_none()
                        {
                            eprintln!(
                                "d1: FILL NOT BOOKED book={:?} instrument={:?} side={:?} qty_e2={} px_e9={} -- unknown book/instrument or overflow; firm position is now understated",
                                fill.book, fill.instrument, fill.side, fill.qty_e2, fill.px_e9
                            );
                        } else {
                            println!(
                                "d1: fill qty_e2={} px_e9={} book={:?} instrument={:?}",
                                fill.qty_e2, fill.px_e9, fill.book, fill.instrument
                            );
                        }
                    }
                    // ponytail: log-and-drop on a full ring, same ceiling as
                    // every other ring in this binary -- a single demo
                    // session, not a backpressure protocol yet.
                    match store.get(event.cl_ord_id) {
                        Some(order) => {
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
                        // Unreachable while `apply_exec` resolves the same
                        // id it just returned Ok for -- but if that ever
                        // stops holding, the report must not vanish mutely.
                        None => eprintln!(
                            "d1: apply_exec succeeded but order is gone from the store, ExecutionReport not published"
                        ),
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

/// Cross/order reference price for a netting cycle (ADR-005 §4 default:
/// arrival mid). Falls back to `last_px_e9` if either side of the book is
/// unpriced (e.g. a two-sided quote hasn't ticked yet), and to `0` if the
/// instrument has never ticked at all -- `d1-netting::net` itself has no
/// opinion on price validity, so an unpriced instrument nets at 0 rather
/// than blocking the cycle.
fn arrival_mid_px_e9(market_data: &MarketData, instrument: InstrumentId) -> i64 {
    let Some(quote) = market_data.quote(instrument) else {
        return 0;
    };
    if quote.bid_px_e9 > 0 && quote.ask_px_e9 > 0 {
        if let Some(sum) = quote.bid_px_e9.checked_add(quote.ask_px_e9) {
            return sum / 2;
        }
    }
    quote.last_px_e9
}
