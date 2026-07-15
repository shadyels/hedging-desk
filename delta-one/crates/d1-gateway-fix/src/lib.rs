//! d1-gateway-fix — see delta-one/CLAUDE.md for the crate's role and rules.
//!
//! FIX 4.4 session (via the `quickfix` C++ binding, ADR-003) plus the two
//! `rtrb` rings (ADR-013) connecting it to the core thread: outbound
//! `d1_core::Order` -> `NewOrderSingle` (35=D), inbound `ExecutionReport`
//! (35=8) -> `d1_core::ExecEvent`. Edge crate, off the hot path.

pub mod convert;
pub mod error;

use std::path::Path;
use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use d1_core::{ExecEvent, Order};
use quickfix::{
    Application, ApplicationCallback, ConnectionHandler, FieldMap, FixSocketServerKind, Initiator,
    LogFactory, MemoryMessageStoreFactory, Message, MsgFromAppError, SessionId, SessionSettings,
    StdLogger, send_to_target,
};

pub use error::FixError;

/// Poll/backoff interval for the outbound-ring drain loop and the
/// `Empty`-ring backoff. Off the hot path (ADR-004): the FIX socket send
/// already isn't part of the 10-50 µs budget, so a short sleep here costs
/// nothing that matters.
const DRAIN_POLL_INTERVAL: Duration = Duration::from_millis(10);

/// `ApplicationCallback` impl: converts inbound `ExecutionReport`s and pushes
/// them onto the inbound-exec ring. Owns only the ring producer -- the
/// `SessionSettings`/store/log factories and the `Initiator` itself can't be
/// bundled into this struct without a self-referential lifetime (they all
/// borrow each other), so `run_initiator` below keeps them as local
/// variables in one stack frame instead (delta-one/CLAUDE.md: "don't fight
/// the borrow checker, restructure instead").
pub struct FixCallbacks {
    inbound_tx: Mutex<rtrb::Producer<ExecEvent>>,
    logged_on: AtomicBool,
}

impl FixCallbacks {
    /// Wrap the producer half of the inbound-exec ring.
    #[must_use]
    pub fn new(inbound_tx: rtrb::Producer<ExecEvent>) -> Self {
        Self {
            inbound_tx: Mutex::new(inbound_tx),
            logged_on: AtomicBool::new(false),
        }
    }

    /// Whether the session has completed its Logon handshake. `run_initiator`
    /// gates sending on this -- a `NewOrderSingle` sent before Logon
    /// completes is an invalid session state and gets the connection killed
    /// (observed: `quickfix` disconnects immediately in that case).
    #[must_use]
    pub fn is_logged_on(&self) -> bool {
        self.logged_on.load(Ordering::Relaxed)
    }
}

impl ApplicationCallback for FixCallbacks {
    fn on_logon(&self, _session: &SessionId) {
        self.logged_on.store(true, Ordering::Relaxed);
    }

    fn on_logout(&self, _session: &SessionId) {
        self.logged_on.store(false, Ordering::Relaxed);
    }

    fn on_msg_from_app(&self, msg: &Message, _session: &SessionId) -> Result<(), MsgFromAppError> {
        // Only ExecutionReport is expected in Slice 2's message set
        // (ADR-003: 35=D/8/F/G); anything else is silently ignored rather
        // than rejected, since D1 only ever sends 35=D on this session.
        // MsgType (35) lives in the header, not the body -- Message::get_field
        // only reads body fields.
        if msg.with_header(|h| h.get_field(35)).as_deref() != Some("8") {
            return Ok(());
        }

        let event = convert::exec_report_to_event(msg).map_err(fix_error_to_from_app)?;

        // ponytail: ring sized generously for a single demo session; a full
        // ring (or a poisoned `Mutex` -- the `Err` arm of `lock()` below)
        // silently drops the exec rather than blocking this callback thread
        // (which the quickfix engine owns). A dropped fill is invisible from
        // here -> the order can stall mid-execution forever. Ceiling for the
        // demo; upgrade to a drop counter / `eprintln!` for observability, or
        // promote to a returned reject (backpressure), when a live venue
        // drives this.
        if let Ok(mut tx) = self.inbound_tx.lock() {
            let _ = tx.push(event);
        }
        Ok(())
    }
}

fn fix_error_to_from_app(err: FixError) -> MsgFromAppError {
    match err {
        FixError::MissingField(_) => MsgFromAppError::FieldNotFound,
        FixError::UnknownStatus(_) => MsgFromAppError::IncorrectTagValue,
        FixError::ExecIdTooLong(_)
        | FixError::ClOrdIdTooLong(_)
        | FixError::Parse { .. }
        | FixError::Fix(_) => MsgFromAppError::IncorrectDataFormat,
    }
}

/// Run the FIX initiator until `shutdown` is set: connects per the session
/// config at `settings_path`, drains `outbound_rx` (one order -> one 35=D)
/// while `callbacks` forwards inbound execs onto its ring. Blocks the
/// calling thread -- this is the "drain thread" side of the architecture
/// diagram; spawn it from `crates/d1`.
pub fn run_initiator(
    settings_path: &Path,
    sender_comp_id: &str,
    target_comp_id: &str,
    callbacks: &FixCallbacks,
    mut outbound_rx: rtrb::Consumer<Order>,
    shutdown: &AtomicBool,
) -> Result<(), FixError> {
    let settings = SessionSettings::try_from_path(settings_path)?;
    let store_factory = MemoryMessageStoreFactory::new();
    let log_factory = LogFactory::try_new(&StdLogger::Stdout)?;
    let app = Application::try_new(callbacks)?;
    let mut initiator = Initiator::try_new(
        &settings,
        &app,
        &store_factory,
        &log_factory,
        FixSocketServerKind::SingleThreaded,
    )?;
    let session_id = SessionId::try_new("FIX.4.4", sender_comp_id, target_comp_id, "")?;

    initiator.start()?;

    while !shutdown.load(Ordering::Relaxed) {
        // Sending before Logon completes is an invalid session state (see
        // `FixCallbacks::is_logged_on` doc) -- wait rather than pop, so the
        // order stays queued on the ring until the session is actually up.
        if !callbacks.is_logged_on() {
            std::thread::sleep(DRAIN_POLL_INTERVAL);
            continue;
        }
        // ponytail: log-and-drop -- a non-convertible order or a transient
        // send failure drops just that order and keeps the session alive. Not
        // retried or dead-lettered: fine for Slice 2 (single CLI startup
        // order, no netting), upgrade to bounded-retry / dead-letter when a
        // real order source lands. `eprintln!` matches the slow-path logging
        // style (`main.rs`); this is an edge crate off the hot path, so the
        // no-logging rule (hot-path only) does not apply.
        match outbound_rx.pop() {
            Ok(order) => match convert::order_to_new_order_single(&order) {
                Ok(msg) => {
                    if let Err(err) = send_to_target(msg, &session_id) {
                        eprintln!("fix: send_to_target failed, dropping order: {err}");
                    }
                }
                Err(err) => eprintln!("fix: order conversion failed, dropping order: {err}"),
            },
            Err(rtrb::PopError::Empty) => std::thread::sleep(DRAIN_POLL_INTERVAL),
        }
    }

    initiator.stop()?;
    Ok(())
}
