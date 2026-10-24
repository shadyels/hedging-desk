//! Delta One core-thread wiring, shared by the `d1` binary (`main.rs`) and
//! its integration tests (`tests/fix_round_trip.rs`, `tests/nats_round_trip.rs`)
//! so both exercise the real ring/thread setup instead of a hand-duplicated
//! copy. `docs/ROADMAP.md` P1.M2 slice 3: the feed-ingest ring/thread
//! (deferred from Slice 2) plus the NATS target/exec-report rings.

pub mod config;
pub mod cycle;
pub mod feed;
pub mod posttrade;
pub mod tickfile; // beside `feed`: the tick-file feed producer's counterpart

use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use config::{LimitsConfig, TrackerConfig};
use cycle::{NettingSession, allocate_fill};
use d1_analytics::{Sample, TrackerRecord, TrackerWindow, analytics};
use d1_core::{
    BookId, ClOrdId, CrossRecord, ExecEvent, ExecOutcome, ExecReport, FeedTick, InstrumentId,
    MarketData, Order, OrderStatus, OrderStore, PositionKeeper, Side, Target, TransferRequest,
};
use d1_gateway_fix::{FixCallbacks, FixError};
use d1_gateway_nats::limits::TierOneLimits;
use d1_gateway_nats::{GatewayConfig, GatewayRings, MarkUpdate, NatsError, PositionUpdate};
use d1_netting::RefPxPolicy;
use d1_posttrade::{AuditOrigin, NettingCycleId, PostTradeError, PostTradeEvent, Stamper};

pub use d1_posttrade::PostTradeConfig;
use d1_refdata::{TrackerBook, Universe};

/// Ring capacity for every `rtrb` ring this binary owns (ADR-013) except the
/// position-mirror ring. Generous for a demo-sized single session, matching
/// `d1-gateway-fix`'s ring sizing.
pub const RING_CAPACITY: usize = 64;
/// Capacity of the position-mirror ring (ADR-014 §7): one `PositionUpdate`
/// per configured book per event, so it needs more headroom than the rest.
pub const POSITION_RING_CAPACITY: usize = 1024;
/// Poll/backoff interval for the core thread's drain loop.
pub const POLL_INTERVAL: Duration = Duration::from_millis(5);

/// `order_audit.detail` flag for an exec whose `LastQty` exceeded leaves.
const OVERFILL_AUDIT_DETAIL: &str =
    "OVERFILL: venue LastQty exceeded leaves; excess not booked (see d1 log)";

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
    /// The feed-ingest producer thread -- `feed::run_feed_producer` (the
    /// synthetic random walk) when `spawn`'s `feed_ticks` is `None`,
    /// `feed::run_tick_file_producer` (a parsed scenario tick file) when it
    /// is `Some`.
    pub feed: JoinHandle<()>,
    /// The Kafka post-trade producer thread, `Some` only when a broker
    /// address was given (`posttrade_cfg`) -- `None` in tests, which run
    /// without a broker.
    pub posttrade: Option<JoinHandle<Result<(), PostTradeError>>>,
    /// The producer thread's OWN shutdown flag, `Some` iff `posttrade` is
    /// `Some`. Deliberately separate from the `shutdown` flag passed into
    /// `spawn`: the core thread is the only pusher onto the `posttrade`
    /// ring, so signalling this flag before `core` has been joined lets the
    /// producer wake, drain an empty ring, flush and exit while the core
    /// thread is still mid-iteration pushing events -- those are then lost.
    /// Ordered-shutdown contract for callers: store `true` into the
    /// `shutdown` passed to `spawn` (stops core/fix/nats/feed), join
    /// `core`, and ONLY THEN store `true` here before joining `posttrade`.
    /// `main.rs` and `tests/golden_posttrade.rs` follow this.
    pub posttrade_shutdown: Option<Arc<AtomicBool>>,
}

/// Which feed producer `spawn` starts for a session, resolved once from
/// `spawn`'s `feed_ticks` parameter (its `feed_source` binding) before
/// `instrument_ids`/`feed_ticks` move into their respective threads/arms.
enum FeedSource {
    /// `feed::run_feed_producer`'s synthetic random walk.
    Synthetic {
        /// Every instrument in the keeper universe -- root CLAUDE.md
        /// invariant 2: an instrument that never ticks prices its internal
        /// crosses at `ref_px_e9 = 0`.
        instruments: Vec<InstrumentId>,
        /// `0` in deterministic mode (the golden-file path), `feed::DRIFT_E9`
        /// otherwise.
        drift_e9: i64,
    },
    /// `feed::run_tick_file_producer`, replaying a pre-parsed scenario tick
    /// file (`crates/d1/src/tickfile.rs`).
    TickFile(Vec<FeedTick>),
}

/// Everything `spawn` needs: the CLI startup order, the FIX/NATS/Kafka
/// endpoints, the refdata universe and the parsed `d1.toml`. A struct, not a
/// dozen loose arguments, so adding a knob does not touch every caller.
pub struct SpawnConfig {
    /// The CLI-driven startup order (see [`StartupOrder`]). Its
    /// `book`/`instrument` must be in `book_ids`/`instrument_ids` for the
    /// order to book anywhere -- the wildcard-target guard in `run_core`
    /// already handles a pair that isn't configured, so `spawn` does not
    /// re-validate that here.
    pub startup: StartupOrder,
    /// FIX session identity + settings file.
    pub fix: FixConfig,
    /// NATS server URL.
    pub nats_url: String,
    /// Keeper/market-data book universe (loaded from
    /// `protocol/refdata/universe.json` via `d1-refdata` by the caller).
    pub book_ids: Vec<BookId>,
    /// Keeper/market-data instrument universe.
    pub instrument_ids: Vec<InstrumentId>,
    /// Cross reference-price policy (ADR-005 §4), parsed and validated by the
    /// caller at startup.
    pub policy: RefPxPolicy,
    /// Handed whole to the Kafka producer thread, where
    /// `d1_posttrade::Schemas::encode` resolves `symbol`/`currency` from it,
    /// separately from `book_ids`/`instrument_ids` above (those feed the
    /// keeper).
    pub universe: Universe,
    /// `d1.toml [tracker]`.
    pub tracker: TrackerConfig,
    /// `d1.toml [limits]` (ADR-008 Tier-1 caps).
    pub limits: LimitsConfig,
    /// `Some(PostTradeConfig { brokers, registry_url })` spawns the Kafka
    /// producer thread, `None` skips it entirely -- tests pass `None` since
    /// they run without a broker; the `posttrade` ring then simply fills and
    /// the log-and-drop push helper in `run_core` drops harmlessly. Broker and
    /// registry travel together because a producer that cannot resolve
    /// Confluent schema ids would publish records no consumer can decode
    /// (ADR-002), so "Kafka without a registry" is deliberately
    /// unconstructable.
    pub posttrade: Option<PostTradeConfig>,
    /// Selects the golden-file reproducible path: `Stamper::Fixed` (instead of
    /// `Stamper::Wall`) for ids/timestamps, and `0` feed drift (instead of
    /// `feed::DRIFT_E9`) so every tick's mid stays pinned at
    /// `feed::STARTING_PX_E9`. Two independent `Stamper`s are built from this
    /// one flag -- one for `NettingSession` in the core thread (`cross_id`),
    /// one moved into the Kafka producer thread
    /// (`msg_id`/`trade_id`/etc). Their `next` bases are deliberately disjoint
    /// (core starts at `1_000_000`, the producer at `1`) so a `cross_id` can
    /// never collide with an unrelated `msg_id` in the golden fixtures -- a
    /// `grep` for one id space should never spuriously hit the other.
    pub deterministic: bool,
    /// Selects the feed source: `None` runs `feed::run_feed_producer` (the
    /// synthetic random walk), `Some(ticks)` runs
    /// `feed::run_tick_file_producer` over an already-parsed scenario tick
    /// file (`crates/d1/src/tickfile.rs`). A pre-parsed `Vec<FeedTick>` rather
    /// than a `PathBuf`: file/parse errors then become a hard startup error in
    /// `main.rs` alongside the `universe`/`tracker`/`policy` gates, instead of
    /// dying silently inside a `JoinHandle<()>`, and it keeps `spawn` itself
    /// I/O-free. `Some(_)` overrides `deterministic`'s feed drift entirely --
    /// drift is meaningless when prices come from a file, not a running walk
    /// -- and `run_core`'s t=0 prime still runs for EVERY instrument
    /// regardless of feed source, so an instrument the tick file never
    /// mentions still has a priced quote for crosses to reference.
    pub feed_ticks: Option<Vec<FeedTick>>,
}

/// Build the `rtrb` rings (ADR-013) and spawn the core/FIX/NATS/feed
/// threads. Blocks on nothing itself -- the caller decides how/when to flip
/// `shutdown` and joins the returned handles. If a Kafka producer thread is
/// spawned (`cfg.posttrade: Some`), it does NOT share `shutdown` -- see
/// `RunHandles::posttrade_shutdown` for the ordered-shutdown contract that
/// protects the producer's final drain. See [`SpawnConfig`] for each input.
#[must_use]
pub fn spawn(cfg: SpawnConfig, shutdown: &Arc<AtomicBool>) -> RunHandles {
    let SpawnConfig {
        startup,
        fix: fix_cfg,
        nats_url,
        book_ids,
        instrument_ids,
        policy,
        universe,
        tracker: tracker_cfg,
        limits,
        posttrade: posttrade_cfg,
        deterministic,
        feed_ticks,
    } = cfg;
    let (fix_outbound_tx, fix_outbound_rx) = rtrb::RingBuffer::<Order>::new(RING_CAPACITY);
    let (fix_inbound_tx, fix_inbound_rx) = rtrb::RingBuffer::<ExecEvent>::new(RING_CAPACITY);
    let (target_tx, target_rx) = rtrb::RingBuffer::<Target>::new(RING_CAPACITY);
    let (exec_report_tx, exec_report_rx) = rtrb::RingBuffer::<ExecReport>::new(RING_CAPACITY);
    let (feed_tx, feed_rx) = rtrb::RingBuffer::<FeedTick>::new(RING_CAPACITY);
    let (cross_tx, cross_rx) = rtrb::RingBuffer::<CrossRecord>::new(RING_CAPACITY);
    let (transfer_tx, transfer_rx) = rtrb::RingBuffer::<TransferRequest>::new(RING_CAPACITY);
    let (posttrade_tx, posttrade_rx) = rtrb::RingBuffer::<PostTradeEvent>::new(RING_CAPACITY);
    // 9th ring (ADR-010, P1.M5 Slice 2): core -> NATS, one `TrackerRecord`
    // per tracker book per sample.
    let (tracker_tx, tracker_rx) = rtrb::RingBuffer::<TrackerRecord>::new(RING_CAPACITY);
    // 10th ring (ADR-014 §2): core -> NATS, one `MarkUpdate` per in-universe
    // feed tick; the gateway throttles and change-detects. 11th ring
    // (ADR-014 §7): core -> NATS, absolute `PositionUpdate`s.
    let (mark_tx, mark_rx) = rtrb::RingBuffer::<MarkUpdate>::new(RING_CAPACITY);
    let (position_tx, position_rx) =
        rtrb::RingBuffer::<PositionUpdate>::new(POSITION_RING_CAPACITY);

    // `next: 1_000_000` -- disjoint from the Kafka producer's `Stamper`
    // below (`next: 1`), see this function's doc comment.
    let core_stamper = if deterministic {
        Stamper::Fixed { next: 1_000_000 }
    } else {
        Stamper::Wall
    };
    // Resolved once here, before `instrument_ids` moves into the core
    // thread below and `feed_ticks` moves into `feed_source`: which feed
    // producer this session runs, and everything its arm needs, so the feed
    // thread's spawn site (further down) is a single match with no
    // `Option`/`unwrap_or_default` left over. Computed here rather than
    // lexically inside the `Synthetic` arm at that spawn site only because
    // `instrument_ids` has already moved into the core thread's closure by
    // the time that site runs.
    let feed_source = match feed_ticks {
        Some(ticks) => FeedSource::TickFile(ticks),
        None => FeedSource::Synthetic {
            // Every instrument in the keeper universe, not just the CLI
            // startup order's, or crosses on the others price at
            // `ref_px_e9 = 0`.
            instruments: instrument_ids.clone(),
            drift_e9: if deterministic { 0 } else { feed::DRIFT_E9 },
        },
    };
    // `universe` moves whole into the Kafka producer thread below (P1.M4
    // Slice 2) -- the core thread and the NATS gateway thread both need a
    // tracker-book slice of it too, so clone just what each needs BEFORE
    // that move, same precedent as `instrument_ids.clone()` inside
    // `feed_source` above.
    let tracker_books = universe.tracker_books.clone();
    let cash_yield_annual_e9 = universe.cash_yield_annual_e9;
    // Resolved once here (the only place with both `tracker_books` and
    // `symbol_to_id` in scope before `universe` moves): each tracker book's
    // `benchmark_symbol` -> the matching instrument id, if the demo universe
    // happens to list the benchmark itself as a tradeable/quotable
    // instrument (e.g. "SPX"). Handed to the NATS gateway thread so it can
    // populate `TrackerAnalytics.benchmark` without depending on
    // `d1-refdata` itself (`d1_analytics::TrackerRecord` stays id-based on
    // `book` only -- ADR-010's pure-calculator boundary).
    let tracker_benchmarks: Vec<(BookId, InstrumentId)> = tracker_books
        .iter()
        .filter_map(|tb| {
            universe
                .symbol_to_id
                .get(&tb.benchmark_symbol)
                .map(|&id| (tb.book, id))
        })
        .collect();
    let tracker_sampling_interval_s = tracker_cfg.sampling_interval_s;
    // Cloned before `book_ids`/`instrument_ids` move into the core thread.
    let gateway_cfg = GatewayConfig {
        url: nats_url,
        book_ids: book_ids.clone(),
        instrument_ids: instrument_ids.clone(),
        tracker_benchmarks,
        sampling_interval_s: tracker_sampling_interval_s,
        limits: TierOneLimits {
            vega_warn_e9: limits.defaults.vega_warn_e9,
            vega_hard_e9: limits.defaults.vega_hard_e9,
            gamma_warn_qty_e2: limits.defaults.gamma_warn_qty_e2,
            gamma_hard_qty_e2: limits.defaults.gamma_hard_qty_e2,
            rho_warn_e9: limits.defaults.rho_warn_e9,
            rho_hard_e9: limits.defaults.rho_hard_e9,
        },
        valuation_max_age_s: limits.valuation_max_age_s,
    };

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
            posttrade_tx,
            tracker_tx,
            mark_tx,
            position_tx,
            book_ids,
            instrument_ids,
            policy,
            core_stamper,
            tracker_books,
            cash_yield_annual_e9,
            tracker_cfg,
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
            gateway_cfg,
            GatewayRings {
                target_tx,
                exec_rx: exec_report_rx,
                cross_rx,
                transfer_tx,
                tracker_rx,
                mark_rx,
                position_rx,
            },
            &nats_shutdown,
        )
    });

    let feed_shutdown = Arc::clone(shutdown);
    let feed = thread::spawn(move || match feed_source {
        FeedSource::Synthetic {
            instruments,
            drift_e9,
        } => feed::run_feed_producer(&instruments, feed_tx, drift_e9, &feed_shutdown),
        FeedSource::TickFile(ticks) => {
            feed::run_tick_file_producer(&ticks, feed_tx, &feed_shutdown)
        }
    });

    // `posttrade_cfg: None` (tests, no broker available) -- don't spawn: the
    // ring simply fills and `run_core`'s log-and-drop push helper drops
    // harmlessly, same ceiling as every other ring in this binary.
    //
    // The producer gets its OWN shutdown flag (`RunHandles::posttrade_shutdown`
    // doc comment has the full ordered-shutdown contract), deliberately NOT
    // cloned from the `shutdown` this function was handed: the core thread
    // is the only pusher onto `posttrade_tx`, so this flag must not flip
    // until the caller has joined `core`, or the producer can drain-flush-exit
    // while `core` is still mid-iteration pushing events.
    let mut posttrade_shutdown = None;
    let posttrade = posttrade_cfg.map(|cfg| {
        let flag = Arc::new(AtomicBool::new(false));
        posttrade_shutdown = Some(Arc::clone(&flag));
        let producer_stamper = if deterministic {
            Stamper::Fixed { next: 1 }
        } else {
            Stamper::Wall
        };
        thread::spawn(move || {
            let result = d1_posttrade::run_producer(
                &cfg.brokers,
                &cfg.registry_url,
                universe,
                posttrade_rx,
                producer_stamper,
                &flag,
            );
            // Log as soon as the thread dies, not just at `main.rs`'s final
            // join: a broker outage at startup (e.g. `ensure_topics` failing)
            // would otherwise leave the entire post-trade/compliance audit
            // trail silently dropped for the rest of the session with no
            // visibility until shutdown.
            if let Err(ref err) = result {
                eprintln!("d1: Kafka post-trade producer exited with error: {err}");
            }
            result
        })
    });

    RunHandles {
        core,
        fix,
        nats,
        feed,
        posttrade,
        posttrade_shutdown,
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
    mut posttrade_tx: rtrb::Producer<PostTradeEvent>,
    mut tracker_tx: rtrb::Producer<TrackerRecord>,
    mut mark_tx: rtrb::Producer<MarkUpdate>,
    mut position_tx: rtrb::Producer<PositionUpdate>,
    book_ids: Vec<BookId>,
    instrument_ids: Vec<InstrumentId>,
    policy: RefPxPolicy,
    stamper: Stamper,
    tracker_books: Vec<TrackerBook>,
    cash_yield_annual_e9: i64,
    tracker_cfg: TrackerConfig,
    shutdown: &AtomicBool,
) {
    let mut store = OrderStore::new(RING_CAPACITY);
    let mut keeper = PositionKeeper::new(&book_ids, &instrument_ids);
    let mut market_data = MarketData::new(&instrument_ids);
    // Prime every instrument's quote synchronously at t=0, before anything
    // else runs (MEDIUM 2 remediation): the feed thread ticks on its own
    // 500ms timer, so without this seed the arrival mid used to price the
    // first netting cycle's crosses depends on whether that thread's first
    // burst has landed by the time a target is popped off `target_rx` -- a
    // poll-loop scheduling race, not a guarantee. `bid == ask == last ==
    // feed::STARTING_PX_E9` averages to the same arrival mid the feed's own
    // first tick would produce (its symmetric `SPREAD_E9` cancels out), so
    // this does not change the golden values.
    for &instrument in &instrument_ids {
        market_data.ingest(&FeedTick {
            instrument_id: instrument,
            bid_px_e9: feed::STARTING_PX_E9,
            ask_px_e9: feed::STARTING_PX_E9,
            last_px_e9: feed::STARTING_PX_E9,
            exch_ts_ns: 0,
            div_per_share_e9: 0,
        });
    }
    // Seed each tracker book's endowed cash (ADR-010, `refdata`'s
    // `initial_cash_e9`) right next to the price prime above -- both are
    // "establish t=0 state before anything else runs" seams. `None` means
    // `tb.book` is not in this process's keeper universe (refdata listed a
    // tracker book this process wasn't configured for); logged loudly rather
    // than silently leaving that book's cash at 0, which would make every
    // one of its NAV returns a nonsensical 0-cash fiction.
    for tb in &tracker_books {
        if keeper.seed_cash(tb.book, tb.initial_cash_e9).is_none() {
            eprintln!(
                "d1: tracker book cash seed failed book={:?} initial_cash_e9={} -- book not in this process's position universe",
                tb.book, tb.initial_cash_e9
            );
        }
    }
    let mut tracker_states: Vec<TrackerState> = tracker_books
        .iter()
        .map(|tb| TrackerState::new(tb, tracker_cfg.te_window_obs))
        .collect();
    // `sampling_interval_s` is a demo-time "day" (`d1.toml`'s comment) --
    // the boundary check below rides the feed's own synthetic `exch_ts_ns`
    // clock, never the wall clock, so this stays fully deterministic.
    let sampling_interval_ns =
        u64::from(tracker_cfg.sampling_interval_s).saturating_mul(1_000_000_000);
    let mut next_sample_ns = sampling_interval_ns;

    let mut session = NettingSession::new(policy, 2, stamper); // seq 1 is the startup order below
    // Marks the feed arm could not hand to the gateway (ring full). Counted
    // with NO I/O on the tick->order path and printed once after the drain
    // loop.
    let mut marks_dropped: u64 = 0;
    // Same for position-mirror updates the ring could not take (no I/O per
    // event on the core thread).
    let mut positions_dropped: u64 = 0;

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
    push_posttrade(
        &mut posttrade_tx,
        posttrade::order_audit(
            cl_ord_id,
            startup.instrument,
            startup.side,
            OrderStatus::New,
            OrderStatus::New,
            0,
            0,
            startup.qty_e2,
            AuditOrigin::System,
            None,
        ),
    );

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
                // Republish the mid D1 itself would cross at (ADR-014 §2):
                // one implementation of the mid rule. `ingest` says "in
                // universe", not "changed" -- the gateway change-detects, so
                // this stays a plain push. A mid <= 0 is not a mark.
                //
                // ponytail: dropped silently on a full ring -- this is the
                // tick->order path, so no I/O here, only the counter printed
                // after the loop. Upgrade: a drop counter in telemetry
                // (backpressure protocol).
                let mid_px_e9 = arrival_mid_px_e9(&market_data, tick.instrument_id);
                if mid_px_e9 > 0
                    && mark_tx
                        .push(MarkUpdate {
                            instrument: tick.instrument_id,
                            mid_px_e9,
                            feed_ts_ns: tick.exch_ts_ns,
                        })
                        .is_err()
                {
                    marks_dropped = marks_dropped.saturating_add(1);
                }
            }

            // Dividend credit (P1.M5 Slice 2): rides the same tick stream as
            // the price update (`FeedTick::div_per_share_e9`'s doc comment).
            // `None` means the credit was rejected outright (unknown
            // instrument or a cost-basis overflow on some book) -- root
            // CLAUDE.md #2, same fault class as a silently-dropped fill, so
            // this is logged just as loudly as "FILL NOT BOOKED" above.
            if tick.div_per_share_e9 != 0 {
                if keeper
                    .credit_dividend(tick.instrument_id, tick.div_per_share_e9)
                    .is_none()
                {
                    eprintln!(
                        "d1: DIVIDEND NOT CREDITED instrument={:?} div_per_share_e9={} -- firm cash is now understated",
                        tick.instrument_id, tick.div_per_share_e9
                    );
                } else {
                    println!(
                        "d1: dividend credited instrument={:?} div_per_share_e9={}",
                        tick.instrument_id, tick.div_per_share_e9
                    );
                }
            }

            // Tracker analytics sampling driver (ADR-010, P1.M5 Slice 2).
            // Sampled off the feed's own synthetic `exch_ts_ns` clock, never
            // the wall clock, so this stays deterministic. `sampling_interval_ns
            // == 0` (a degenerate `d1.toml` config) disables sampling outright
            // rather than resampling on every tick.
            //
            // The FIRST boundary crossing only establishes each tracker
            // state's `prev_nav_e9`/`prev_mids_e9` baseline (see
            // `TrackerState::sample`) -- a return needs two NAV levels, so
            // reaching `n_obs >= 2` (the minimum `d1_analytics::analytics`
            // needs) takes THREE crossings: #1 seeds the baseline, #2
            // computes the first return (window len 1), #3 computes the
            // second (window len 2, analytics finally `Some`).
            //
            // ponytail: the feed producer ticks every instrument in one
            // "wave" sharing the same `exch_ts_ns` (`crates/d1/src/feed.rs`),
            // so this can fire on the FIRST tick of a wave, before the rest
            // of that wave's instruments have been ingested into
            // `market_data` -- a demo-scale approximation, not a windowed
            // barrier. Acceptable here because the ring drains faster than
            // the 500ms feed cadence; a real feed would need an explicit
            // "wave complete" signal instead.
            if sampling_interval_ns > 0 && tick.exch_ts_ns >= next_sample_ns {
                for state in &mut tracker_states {
                    // Cash must actually earn the configured yield, or cash
                    // sits at 0% in NAV while ADR-010's cash-drag formula
                    // assumes it earns `cash_yield_annual_e9` -- an
                    // internally-inconsistent report. `None` means the
                    // book isn't in this process's keeper universe (already
                    // logged loudly at the cash-seed step above) or the
                    // accrual overflowed; either way this sample's NAV would
                    // be silently wrong, so it is logged just as loudly.
                    if keeper
                        .accrue_cash(
                            state.book,
                            cash_yield_annual_e9,
                            d1_analytics::TRADING_DAYS_PER_YEAR,
                        )
                        .is_none()
                    {
                        eprintln!(
                            "d1: CASH ACCRUAL NOT APPLIED book={:?} -- tracker cash yield understated this sample",
                            state.book
                        );
                    }

                    state.sample(&keeper, &market_data, tick.exch_ts_ns);

                    if let Some(record) = analytics(state.book, &state.window, cash_yield_annual_e9)
                    {
                        if tracker_tx.push(record).is_err() {
                            eprintln!(
                                "d1: tracker ring full, dropping TrackerRecord book={:?}",
                                state.book
                            );
                        } else {
                            println!(
                                "d1: tracker sample book={:?} n_obs={} te_ann_e9={} td_e9={} cash_drag_e9={}",
                                record.book,
                                record.n_obs,
                                record.tracking_error_ann_e9,
                                record.tracking_diff_e9,
                                record.cash_drag_e9
                            );
                        }
                    }
                }
                // Catches up to the tick's actual clock instead of advancing
                // by exactly one interval (L3 remediation): if tick spacing
                // ever exceeds `sampling_interval_ns`, the old
                // `next_sample_ns + interval` form would fire every
                // subsequent tick against an unchanged boundary -- zero-length
                // periods, zero returns, TE silently diluted toward zero.
                next_sample_ns =
                    (tick.exch_ts_ns / sampling_interval_ns + 1) * sampling_interval_ns;
            }
        }

        if let Ok(target) = target_rx.pop() {
            did_work = true;
            // The keeper's universe is the gate: `exo.targets.>` is a
            // wildcard, so a target can name a (book, instrument) this
            // process was never configured for. Placing that order would
            // fill at the venue and then have nowhere to book -- a position
            // lost in silence (root CLAUDE.md #2). Reject it instead.
            let ref_px_e9 = arrival_mid_px_e9(&market_data, target.instrument);
            match (keeper.position(target.book, target.instrument), ref_px_e9) {
                (None, _) => eprintln!(
                    "d1: target for unconfigured book={:?} instrument={:?}, rejecting (not in this process's position universe)",
                    target.book, target.instrument
                ),
                // Rejected whole, before `on_target`: it builds residual
                // weights from ALL crosses, so dropping one cross mid-cycle
                // would unbalance books.
                (Some(_), px) if px <= 0 => eprintln!(
                    "d1: target book={:?} instrument={:?} ref_px_e9={px}, rejecting (non-positive reference price)",
                    target.book, target.instrument
                ),
                (Some(_), ref_px_e9) => {
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
                                // Ledger first, NATS notice second. The cross
                                // is already booked in `keeper` regardless of
                                // whether the notice makes it out, so the
                                // post-trade audit trail must not depend on
                                // that push -- and ordering it first is what
                                // lets an observer treat the NATS notice as
                                // proof the post-trade events are already
                                // enqueued (`tests/golden_posttrade.rs` syncs
                                // on exactly that).
                                for event in posttrade::cross_events(
                                    record,
                                    NettingCycleId::Cycle(output.cycle_id),
                                ) {
                                    push_posttrade(&mut posttrade_tx, event);
                                }
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
                                    push_posttrade(
                                        &mut posttrade_tx,
                                        posttrade::order_audit(
                                            order.cl_ord_id,
                                            order.instrument,
                                            order.side,
                                            OrderStatus::New,
                                            OrderStatus::New,
                                            0,
                                            0,
                                            order.order_qty_e2,
                                            AuditOrigin::NettingEngine,
                                            None,
                                        ),
                                    );
                                }
                            }
                            positions_dropped = positions_dropped.saturating_add(push_positions(
                                &mut position_tx,
                                &keeper,
                                &session,
                                target.instrument,
                                &book_ids,
                            ));
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
            let ref_px_e9 = arrival_mid_px_e9(&market_data, transfer.instrument);
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
            } else if ref_px_e9 <= 0 {
                // Same whole-reject-before-`on_transfer` rule as targets.
                eprintln!(
                    "d1: transfer from_book={:?} to_book={:?} instrument={:?} ref_px_e9={ref_px_e9}, rejecting (non-positive reference price)",
                    transfer.from_book, transfer.to_book, transfer.instrument
                );
            } else if let Some(record) = session.on_transfer(transfer, &mut keeper, ref_px_e9) {
                println!(
                    "d1: booked directed transfer cross_id={} instrument={:?} buy_book={:?} sell_book={:?} qty_e2={}",
                    record.cross_id,
                    record.instrument,
                    record.buy_book,
                    record.sell_book,
                    record.qty_e2
                );
                // Ledger first, NATS notice second -- same ordering
                // rationale as the netting-derived cross path above.
                for event in posttrade::cross_events(&record, NettingCycleId::Direct) {
                    push_posttrade(&mut posttrade_tx, event);
                }
                if cross_tx.push(record).is_err() {
                    eprintln!(
                        "d1: cross ring full, dropping InternalCrossNotice cross_id={}",
                        record.cross_id
                    );
                }
                positions_dropped = positions_dropped.saturating_add(push_positions(
                    &mut position_tx,
                    &keeper,
                    &session,
                    transfer.instrument,
                    &book_ids,
                ));
            } else {
                eprintln!(
                    "d1: transfer not booked (overflow) instrument={:?} from_book={:?} to_book={:?} qty_e2={} -- not published",
                    transfer.instrument, transfer.from_book, transfer.to_book, transfer.qty_e2
                );
            }
        }

        if let Ok(event) = fix_inbound_rx.pop() {
            did_work = true;
            // Captured before `apply_exec` mutates the order -- the audit
            // trail's `from_status` (P1.M4). `apply_exec`'s own success
            // guarantees this lookup also succeeded (same `cl_ord_id`), so
            // an unexpected `None` here can only mean the store and the
            // exec disagree about the order's existence.
            let from_status = store.get(event.cl_ord_id).map(|o| o.status);
            let outcome = store.apply_exec(&event);
            let excess_qty_e2 = match outcome {
                Ok(ExecOutcome::Overfilled { excess_qty_e2, .. }) => excess_qty_e2,
                _ => 0,
            };
            match outcome {
                // A redelivered `ExecId` (FIX PossDup after a sequence
                // reset, or a venue re-send -- root CLAUDE.md #4, the exact
                // scenario `OrderStore`'s dedupe exists for): no state
                // changed, so emit nothing -- no audit record, no fill
                // booking, no `ExecutionReport` republish. Emitting any of
                // those here would mint a fresh, undedupable record for an
                // exec that already landed once.
                Ok(ExecOutcome::Duplicate) => {}
                Ok(ExecOutcome::Applied(fill) | ExecOutcome::Overfilled { fill, .. }) => {
                    let booked_qty_e2 = fill.map_or(0, |f| f.qty_e2);
                    let detail = (excess_qty_e2 > 0).then_some(OVERFILL_AUDIT_DETAIL);
                    if excess_qty_e2 > 0 {
                        eprintln!(
                            "d1: OVERFILL EXCESS NOT BOOKED cl_ord_id={:?} exec_id={:?} excess_qty_e2={excess_qty_e2} last_qty_e2={} px_e9={} -- venue filled more than the order; firm position now differs from the venue's by the excess",
                            String::from_utf8_lossy(&event.cl_ord_id.0),
                            String::from_utf8_lossy(event.exec_id.unpadded()),
                            event.last_qty_e2,
                            event.last_px_e9
                        );
                    }
                    // Post-`apply_exec` snapshot, reused below both for the
                    // audit trail's cum/leaves and for the `ExecReport`.
                    let current = store.get(event.cl_ord_id);
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
                            let cycle_id = parent.cycle_id;
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
                                    // Per-book allocation report (ADR-014 §5),
                                    // only for an allocation that BOOKED, so
                                    // EXO never counts a fill D1 did not.
                                    // Pushed before the parent's book-0 report
                                    // below, on the same ring, so it publishes
                                    // first.
                                    //
                                    // ponytail: carries the PARENT's
                                    // status/cum/leaves and no allocation
                                    // flag -- no per-book cumulative until
                                    // P3.M1's blotter needs one.
                                    if let Some(order) = current {
                                        let report = ExecReport {
                                            cl_ord_id: event.cl_ord_id,
                                            exec_id: event.exec_id,
                                            book,
                                            instrument,
                                            side,
                                            status: order.status,
                                            last_qty_e2: qty_e2,
                                            last_px_e9: fill.px_e9,
                                            cum_qty_e2: order.cum_qty_e2,
                                            leaves_qty_e2: order.leaves_qty_e2,
                                        };
                                        if exec_report_tx.push(report).is_err() {
                                            eprintln!(
                                                "d1: NATS outbound ring full, dropping allocation ExecutionReport book={book:?}"
                                            );
                                        }
                                    }
                                    push_posttrade(
                                        &mut posttrade_tx,
                                        posttrade::allocation_event(
                                            event.cl_ord_id,
                                            event.exec_id,
                                            instrument,
                                            book,
                                            qty_e2,
                                            fill.px_e9,
                                            NettingCycleId::Cycle(cycle_id),
                                        ),
                                    );
                                }
                            }
                            push_posttrade(
                                &mut posttrade_tx,
                                posttrade::external_fill_trade(
                                    BookId(0),
                                    instrument,
                                    side,
                                    fill.qty_e2,
                                    fill.px_e9,
                                    event.exec_id,
                                    event.cl_ord_id,
                                ),
                            );
                            if let (Some(from_status), Some(order)) = (from_status, current) {
                                push_posttrade(
                                    &mut posttrade_tx,
                                    posttrade::order_audit(
                                        event.cl_ord_id,
                                        instrument,
                                        side,
                                        from_status,
                                        order.status,
                                        fill.qty_e2,
                                        order.cum_qty_e2,
                                        order.leaves_qty_e2,
                                        AuditOrigin::System,
                                        detail,
                                    ),
                                );
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
                            push_posttrade(
                                &mut posttrade_tx,
                                posttrade::external_fill_trade(
                                    fill.book,
                                    fill.instrument,
                                    fill.side,
                                    fill.qty_e2,
                                    fill.px_e9,
                                    event.exec_id,
                                    event.cl_ord_id,
                                ),
                            );
                            if let (Some(from_status), Some(order)) = (from_status, current) {
                                push_posttrade(
                                    &mut posttrade_tx,
                                    posttrade::order_audit(
                                        event.cl_ord_id,
                                        fill.instrument,
                                        fill.side,
                                        from_status,
                                        order.status,
                                        fill.qty_e2,
                                        order.cum_qty_e2,
                                        order.leaves_qty_e2,
                                        AuditOrigin::System,
                                        detail,
                                    ),
                                );
                            }
                        }
                    } else if let (Some(from_status), Some(order)) = (from_status, current) {
                        // A non-fill terminal exec (reject/cancel/expire), or an overfill
                        // with nothing left to book:
                        // no quantity moved, so no `TradeLeg`/allocation, but
                        // the state transition itself must still land on the
                        // audit topic -- a compliance stream that silently
                        // omits rejects is a misleading record.
                        push_posttrade(
                            &mut posttrade_tx,
                            posttrade::order_audit(
                                event.cl_ord_id,
                                order.instrument,
                                order.side,
                                from_status,
                                order.status,
                                booked_qty_e2,
                                order.cum_qty_e2,
                                order.leaves_qty_e2,
                                AuditOrigin::System,
                                detail,
                            ),
                        );
                    }
                    // ponytail: log-and-drop on a full ring, same ceiling as
                    // every other ring in this binary -- a single demo
                    // session, not a backpressure protocol yet.
                    match current {
                        Some(order) => {
                            let report = ExecReport {
                                cl_ord_id: event.cl_ord_id,
                                exec_id: event.exec_id,
                                book: order.book,
                                instrument: order.instrument,
                                side: order.side,
                                status: order.status,
                                last_qty_e2: booked_qty_e2,
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
                    // Fills move positions and inflight. Refresh the mirror
                    // for this instrument on every applied exec.
                    //
                    // ponytail: a terminal (rejected/cancelled/expired)
                    // netting parent is never retired from
                    // `NettingSession::inflight` -- the session has no API to
                    // retire a parent (`cycle.rs` `parent_mut`/`inflight`) --
                    // so its unfilled share stays in inflight forever. That
                    // distorts netting demand and, now, the published
                    // `PositionSnapshot.inflight_qty_e2`. Pre-existing; upgrade:
                    // retire the parent on a terminal ExecType.
                    if let Some(order) = current {
                        positions_dropped = positions_dropped.saturating_add(push_positions(
                            &mut position_tx,
                            &keeper,
                            &session,
                            order.instrument,
                            &book_ids,
                        ));
                    }
                }
                Err(err) => eprintln!("d1: apply_exec error: {err}"),
            }
        }

        if !did_work {
            thread::sleep(POLL_INTERVAL);
        }
    }

    if positions_dropped > 0 {
        eprintln!(
            "d1: {positions_dropped} position update(s) dropped (ring full) during this session"
        );
    }
    if marks_dropped > 0 {
        eprintln!("d1: {marks_dropped} mark update(s) dropped (ring full) during this session");
    }

    // P1.M5 Slice 3 (ADR-010 §4): emit exactly one Kafka
    // `posttrade.tracker.analytics` record per tracker book, HERE -- after
    // the drain loop above has exited, never inside it. Two reasons, both
    // load-bearing:
    //
    // 1. Ordering/goldens. `rtrb` is FIFO, so an event pushed after the loop
    //    is provably the LAST event the producer thread ever pops off this
    //    ring, which is what keeps the four existing golden fixtures
    //    byte-identical instead of shifting every `msg_id` in the
    //    `Stamper::Fixed` sequence forward by one. Safe because the
    //    ordered-shutdown contract (`RunHandles::posttrade_shutdown`'s doc
    //    comment) joins `core` -- this function -- before signalling the
    //    producer, so the producer's final `drain()` provably catches this
    //    push.
    // 2. Contract. ADR-010 §4 specifies a DAILY Avro record, and
    //    end-of-session is this demo's "day" -- there is no periodic Kafka
    //    publish at all here; `publish_interval_s` (`d1.toml [tracker]`)
    //    governs only the NATS cadence above, never this one.
    //
    // ponytail: a real deployment triggers this on an end-of-day boundary
    // this process never observes -- the synthetic feed clock has no
    // calendar/wall-clock concept to detect one. Upgrade to a real EOD
    // scheduler when this ever runs against a live feed instead of one demo
    // session.
    for state in &tracker_states {
        match analytics(state.book, &state.window, cash_yield_annual_e9) {
            Some(record) => push_posttrade(
                &mut posttrade_tx,
                PostTradeEvent::Tracker(record, tracker_cfg.sampling_interval_s),
            ),
            // Silent otherwise: a short session (fewer than 2 samples ever
            // pushed into the window) produces no compliance record and, up
            // to this point, no log line explaining why -- every other
            // failure in this driver logs loudly (L1 remediation).
            None => eprintln!(
                "d1: no end-of-session tracker record for book={:?} -- window has {} observation(s), need >= 2",
                state.book,
                state.window.len()
            ),
        }
    }
}

/// Push one post-trade event onto the Kafka producer ring, log-and-drop on
/// full -- same ceiling as every other ring in this binary (a single demo
/// session, not a backpressure protocol yet). With no producer thread
/// running (`spawn`'s `posttrade_cfg: None`, tests), this ring simply fills
/// and every subsequent push drops harmlessly.
fn push_posttrade(tx: &mut rtrb::Producer<PostTradeEvent>, event: PostTradeEvent) {
    if tx.push(event).is_err() {
        eprintln!("d1: posttrade ring full, dropping post-trade event");
    }
}

/// Push one absolute `PositionUpdate` per configured book for `instrument`
/// (ADR-014 §7). Called after every core arm that can change a position or
/// inflight on that instrument. Values are absolute, so a later update heals
/// a dropped one. Returns how many updates the ring could not take; the caller
/// counts them (no per-event I/O on the core thread) and prints the total once
/// after the loop.
///
/// ponytail: a drop is counted, not resynced -- a full ring leaves the mirror
/// stale until the next event on that instrument. The ring is sized 1024 to
/// make that unlikely, not impossible. Upgrade: re-push the whole book when
/// the counter is non-zero (the values are absolute, so one pass heals it),
/// or the backpressure protocol.
#[must_use]
fn push_positions(
    tx: &mut rtrb::Producer<PositionUpdate>,
    keeper: &PositionKeeper,
    session: &NettingSession,
    instrument: InstrumentId,
    book_ids: &[BookId],
) -> u64 {
    let mut dropped = 0u64;
    for &book in book_ids {
        let Some(position) = keeper.position(book, instrument) else {
            continue; // not in this process's universe: nothing to mirror
        };
        let inflight_qty_e2 = match session.inflight(instrument, book) {
            Ok(inflight) => inflight,
            Err(err) => {
                eprintln!(
                    "d1: position mirror skipped book={book:?} instrument={instrument:?}: inflight {err}"
                );
                continue;
            }
        };
        let update = PositionUpdate {
            book,
            instrument,
            qty_e2: position.net_qty_e2,
            avg_px_e9: position.avg_px_e9,
            inflight_qty_e2,
        };
        if tx.push(update).is_err() {
            dropped = dropped.saturating_add(1);
        }
    }
    dropped
}

/// Cross/order reference price for a netting cycle (ADR-005 §4 default:
/// arrival mid). Falls back to `last_px_e9` if either side of the book is
/// unpriced (e.g. a two-sided quote hasn't ticked yet), and to `0` if the
/// instrument has never ticked at all. `d1-netting::net` itself has no
/// opinion on price validity, so this function does not reject; `run_core`
/// does -- it drops any target/transfer whose reference price is `<= 0`
/// (`ref_px_e9 <= 0`) before `on_target`/`on_transfer`.
///
/// `pub` (not `pub(crate)`) because it is the cross/order reference-price
/// read shared by the live loop (`run_core`, above) and the in-repo
/// composed tick-to-trade bench, which compiles as an external crate and so
/// cannot see crate-private items.
pub fn arrival_mid_px_e9(market_data: &MarketData, instrument: InstrumentId) -> i64 {
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

/// Per-tracker-book sampling state (ADR-010, P1.M5 Slice 2): its rolling
/// analytics window, the previous sample's NAV and per-constituent mids
/// (`None`/all-zero until the first boundary crossing establishes a
/// baseline -- see `sample`'s doc comment), and its resolved constituent
/// list. One allocated per `Universe::tracker_books` entry at `run_core`
/// startup, never after.
struct TrackerState {
    /// The tracker book this state samples.
    book: BookId,
    /// `(instrument, weight_e9)` pairs, cloned from `d1_refdata::TrackerBook`
    /// once at startup.
    constituents: Vec<(InstrumentId, i64)>,
    /// Rolling window of returns feeding `d1_analytics::analytics`.
    window: TrackerWindow,
    /// Previous sample's book NAV, `_e9`. `None` until the first boundary
    /// crossing (that crossing only establishes this baseline; see `sample`).
    prev_nav_e9: Option<i64>,
    /// Previous sample's mid price per constituent, parallel to
    /// `constituents`, preallocated once at startup and updated in place
    /// each sample (`0` is a valid "never sampled yet" sentinel: mids are
    /// always positive once the feed has ticked, and `period_return_e9`
    /// itself rejects a `0` previous value rather than mistaking it for a
    /// real quote).
    prev_mids_e9: Vec<i64>,
}

impl TrackerState {
    /// Preallocate one tracker book's sampling state: the window (capacity
    /// `te_window_obs`) and the per-constituent mid scratch buffer, sized
    /// once to `tb.constituents.len()` and never reallocated afterward.
    fn new(tb: &TrackerBook, te_window_obs: usize) -> Self {
        Self {
            book: tb.book,
            constituents: tb.constituents.clone(),
            window: TrackerWindow::new(te_window_obs),
            prev_nav_e9: None,
            prev_mids_e9: vec![0; tb.constituents.len()],
        }
    }

    /// Take one sample at `ts_ns`: compute this book's current NAV and cash
    /// weight, and -- if a previous NAV baseline already exists -- the
    /// period's book/benchmark returns, pushing a `Sample` into `window`.
    ///
    /// The FIRST call for a given book only establishes `prev_nav_e9`/
    /// `prev_mids_e9`: there is no earlier level to return FROM yet, so
    /// nothing is pushed. Every call after that computes a return against
    /// the previous call's snapshot and always refreshes the snapshot to
    /// the current one, whether or not a `Sample` was pushed (a failed
    /// return computation -- overflow, a book that fell out of the keeper's
    /// universe -- must not permanently wedge the baseline on stale data).
    fn sample(&mut self, keeper: &PositionKeeper, market_data: &MarketData, ts_ns: u64) {
        let Some(nav_e9) = book_nav_e9(keeper, market_data, self.book) else {
            eprintln!(
                "d1: tracker NAV computation failed (overflow or unconfigured) book={:?}, skipping sample",
                self.book
            );
            return;
        };
        let Some(cash_e9) = keeper.cash(self.book) else {
            eprintln!(
                "d1: tracker cash lookup failed book={:?}, skipping sample",
                self.book
            );
            return;
        };
        let Some(cash_weight_e9) = cash_weight_e9(cash_e9, nav_e9) else {
            eprintln!(
                "d1: tracker cash-weight computation failed (nav=0 or overflow) book={:?}, skipping sample",
                self.book
            );
            return;
        };

        if let Some(prev_nav_e9) = self.prev_nav_e9 {
            match (
                d1_analytics::period_return_e9(prev_nav_e9, nav_e9),
                bench_return_e9(market_data, &self.constituents, &self.prev_mids_e9),
            ) {
                (Some(book_return_e9), Some(bench_return_e9)) => {
                    self.window.push(Sample {
                        window_end_ns: ts_ns,
                        book_return_e9,
                        bench_return_e9,
                        cash_weight_e9,
                    });
                }
                _ => eprintln!(
                    "d1: tracker sample return computation failed (overflow) book={:?}, skipping sample",
                    self.book
                ),
            }
        }

        refresh_constituent_mids(market_data, &self.constituents, &mut self.prev_mids_e9);
        self.prev_nav_e9 = Some(nav_e9);
    }
}

/// One tracker book's current NAV: `Σ_i (net_qty_e2[b,i] · mid_e9[i]) / 100 +
/// cash_e9[b]` (ADR-010 §3), over EVERY instrument in the book's position
/// row (`PositionKeeper::positions_for_book`), not just its benchmark
/// constituents. `i128` accumulator, checked throughout -- `None` on
/// overflow or an unconfigured book, mirroring `d1-core::keeper`'s posture.
fn book_nav_e9(keeper: &PositionKeeper, market_data: &MarketData, book: BookId) -> Option<i64> {
    let mut nav_e9 = i128::from(keeper.cash(book)?);
    for (instrument, position) in keeper.positions_for_book(book)? {
        let mid_e9 = arrival_mid_px_e9(market_data, instrument);
        let notional = i128::from(position.net_qty_e2)
            .checked_mul(i128::from(mid_e9))?
            .checked_div(100)?;
        nav_e9 = nav_e9.checked_add(notional)?;
    }
    i64::try_from(nav_e9).ok()
}

/// `cash_e9 · 1e9 / nav_e9` (ADR-010 §3's `cash_wt_e9`). `None` when
/// `nav_e9 == 0` (no baseline to weight against) or the ratio overflows.
fn cash_weight_e9(cash_e9: i64, nav_e9: i64) -> Option<i64> {
    if nav_e9 == 0 {
        return None;
    }
    let ratio = i128::from(cash_e9)
        .checked_mul(1_000_000_000i128)?
        .checked_div(i128::from(nav_e9))?;
    i64::try_from(ratio).ok()
}

/// The tracker book's fixed-weight benchmark return over one period,
/// `Σ w_c · r_c` (ADR-010 §3), using `prev_mids_e9` as each constituent's
/// starting mid -- read-only here; `refresh_constituent_mids` is the
/// separate commit step (`TrackerState::sample`), called UNCONDITIONALLY
/// after this (and the book return) are attempted, whether or not either one
/// succeeded -- see `sample`'s own doc comment for why a failed return must
/// not permanently wedge the baseline on stale data. Mids come from the
/// existing `arrival_mid_px_e9` helper, per this slice's spec -- no new
/// pricing path. `None` on the first constituent whose return can't be
/// computed (no prior mid yet, or overflow) or on `i128`/`i64` overflow in
/// the weighted sum.
///
/// ponytail: this is a PRICE return (`arrival_mid_px_e9`), while the book's
/// own NAV return (`book_nav_e9`) is a TOTAL return -- it includes cash,
/// which receives dividend credits (`PositionKeeper::credit_dividend`).
/// Constituent dividends therefore still land entirely in tracking
/// difference even after the ex-div price drop (M6 remediation,
/// `crates/d1/src/feed.rs::run_feed_producer`) keeps the book itself from
/// creating value out of nothing. Closing this gap needs a total-return
/// benchmark index (per-constituent dividend data), which the demo universe
/// does not carry -- future work, not this slice's scope.
fn bench_return_e9(
    market_data: &MarketData,
    constituents: &[(InstrumentId, i64)],
    prev_mids_e9: &[i64],
) -> Option<i64> {
    let mut sum_e9: i128 = 0;
    for (idx, (instrument, weight_e9)) in constituents.iter().enumerate() {
        let mid_now_e9 = arrival_mid_px_e9(market_data, *instrument);
        let mid_prev_e9 = *prev_mids_e9.get(idx)?;
        let r_c_e9 = d1_analytics::period_return_e9(mid_prev_e9, mid_now_e9)?;
        let contrib = i128::from(*weight_e9)
            .checked_mul(i128::from(r_c_e9))?
            .checked_div(1_000_000_000i128)?;
        sum_e9 = sum_e9.checked_add(contrib)?;
    }
    i64::try_from(sum_e9).ok()
}

/// Overwrite `prev_mids_e9` in place with each constituent's CURRENT mid --
/// the commit step for `bench_return_e9`'s read, and how the very first
/// sampling boundary crossing establishes its baseline (`TrackerState::sample`'s
/// doc comment). Preallocated buffer, indexed via `.get_mut()` (never `[]`,
/// `indexing_slicing` is deny-level) rather than reallocated.
fn refresh_constituent_mids(
    market_data: &MarketData,
    constituents: &[(InstrumentId, i64)],
    prev_mids_e9: &mut [i64],
) {
    for (idx, (instrument, _weight_e9)) in constituents.iter().enumerate() {
        let mid_now_e9 = arrival_mid_px_e9(market_data, *instrument);
        if let Some(slot) = prev_mids_e9.get_mut(idx) {
            *slot = mid_now_e9;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn zero_and_never_ticked_instruments_have_non_positive_ref_px() {
        let (zero, good, never) = (InstrumentId(1), InstrumentId(2), InstrumentId(3));
        let mut md = MarketData::new(&[zero, good, never]);
        let tick = |instrument_id, px| FeedTick {
            instrument_id,
            bid_px_e9: px,
            ask_px_e9: px,
            last_px_e9: px,
            exch_ts_ns: 1,
            div_per_share_e9: 0,
        };
        md.ingest(&tick(zero, 0)); // `tickfile::parse` now rejects zero prices, but a hand-built tick passed as `spawn(.., Some(ticks))` still reaches `MarketData` unchecked; a never-ticked instrument (below) also reads 0
        md.ingest(&tick(good, 150_000_000_000));
        // The `<= 0` values `run_core` rejects targets/transfers on.
        assert!(arrival_mid_px_e9(&md, zero) <= 0);
        assert!(arrival_mid_px_e9(&md, never) <= 0); // never ticked -> 0
        assert!(arrival_mid_px_e9(&md, good) > 0);
    }

    /// ADR-014 §7: one absolute update per configured book, carrying the
    /// keeper's position and the session's SIGNED inflight; an instrument
    /// outside the universe pushes nothing.
    #[test]
    #[allow(clippy::unwrap_used)] // tests: unwrap_used/expect_used are hot-path-only bans (delta-one/CLAUDE.md)
    fn push_positions_mirrors_keeper_position_and_signed_inflight() {
        let (b1, b2, aapl) = (BookId(1), BookId(2), InstrumentId(1001));
        let mut keeper = PositionKeeper::new(&[b1, b2], &[aapl]);
        keeper
            .apply_fill(b1, aapl, Side::Buy, 100, 150_000_000_000)
            .unwrap();
        let mut session = NettingSession::new(RefPxPolicy::ArrivalMid, 1, Stamper::Wall);
        // Book 2 targets -1000: a Sell parent, so its inflight is NEGATIVE.
        let target = Target {
            book: b2,
            instrument: aapl,
            target_qty_e2: -1_000,
            band_e2: 0,
        };
        session
            .on_target(target, &mut keeper, 150_000_000_000)
            .unwrap();

        let (mut tx, mut rx) = rtrb::RingBuffer::<PositionUpdate>::new(8);
        assert_eq!(
            push_positions(&mut tx, &keeper, &session, aapl, &[b1, b2]),
            0
        );
        let u1 = rx.pop().unwrap();
        let u2 = rx.pop().unwrap();
        assert!(rx.pop().is_err());
        assert_eq!(
            (u1.book, u1.qty_e2, u1.avg_px_e9),
            (b1, 100, 150_000_000_000)
        );
        assert_eq!(u1.inflight_qty_e2, 0);
        assert_eq!((u2.book, u2.qty_e2, u2.inflight_qty_e2), (b2, 0, -1_000));

        let none = push_positions(&mut tx, &keeper, &session, InstrumentId(9999), &[b1, b2]);
        assert_eq!(none, 0);
        assert!(rx.pop().is_err(), "unconfigured instrument pushes nothing");

        // A full ring drops and COUNTS (no I/O): capacity 1, two books -> 1 dropped.
        let (mut tiny_tx, _tiny_rx) = rtrb::RingBuffer::<PositionUpdate>::new(1);
        assert_eq!(
            push_positions(&mut tiny_tx, &keeper, &session, aapl, &[b1, b2]),
            1
        );
    }
}
