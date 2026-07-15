//! FIX 4.4 acceptor mode: stands in for a broker/EMS, filling
//! `NewOrderSingle` orders per a configurable fill model (sim/CLAUDE.md).
//! Session behavior is `quickfix`-spec-correct (sim/CLAUDE.md: "Delta One's
//! session handling is being tested against it"); the fill model is sim's
//! own demo logic, not part of that spec-correctness bar.
//!
//! ponytail: `Delayed` and reject-rate fill models are listed in
//! sim/CLAUDE.md but not implemented here -- Slice 2 only needs enough arms
//! to drive `d1-core`'s order-state-machine transitions (New -> Filled,
//! New -> PartiallyFilled -> Filled, New -> Rejected). Add them if a demo
//! scenario needs delayed/flaky fills.
//!
//! No shared code with `d1-gateway-fix::convert` (sim/CLAUDE.md: sim depends
//! on `d1-core` only, not on production gateway crates) -- the handful of
//! FIX tag reads/writes here are sim's own, small enough not to be worth a
//! cross-crate dependency.

use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc;

use anyhow::{Context, Result, bail};
use quickfix::{
    Acceptor, Application, ApplicationCallback, ConnectionHandler, FieldMap, FixSocketServerKind,
    LogFactory, MemoryMessageStoreFactory, Message, MsgFromAppError, QuickFixError, SessionId,
    SessionSettings, StdLogger, send_to_target,
};

const TAG_MSG_TYPE: i32 = 35;
const TAG_CL_ORD_ID: i32 = 11;
const TAG_ORDER_ID: i32 = 37;
const TAG_EXEC_ID: i32 = 17;
const TAG_EXEC_TYPE: i32 = 150;
const TAG_ORD_STATUS: i32 = 39;
const TAG_SYMBOL: i32 = 55;
const TAG_SIDE: i32 = 54;
const TAG_ORDER_QTY: i32 = 38;
const TAG_LAST_QTY: i32 = 32;
const TAG_LAST_PX: i32 = 31;
const TAG_CUM_QTY: i32 = 14;
const TAG_LEAVES_QTY: i32 = 151;

/// Demo fill price for every exec. ponytail: sim's acceptor has no live book
/// in this mode (that's `replay`'s job); a real fill-price model is future
/// work if a scenario ever needs acceptor mode and price movement together.
const FILL_PX_E9: i64 = 100_000_000_000; // 100.000000000

/// How sim's acceptor responds to a `NewOrderSingle`.
#[derive(Debug, Clone, Copy)]
pub enum FillModel {
    /// One exec: New -> Filled.
    ImmediateFull,
    /// Two execs: New -> PartiallyFilled -> Filled.
    Partial,
    /// One exec: New -> Rejected.
    Reject,
}

impl std::str::FromStr for FillModel {
    type Err = anyhow::Error;

    fn from_str(s: &str) -> Result<Self> {
        match s {
            "immediate" => Ok(Self::ImmediateFull),
            "partial" => Ok(Self::Partial),
            "reject" => Ok(Self::Reject),
            other => bail!("unknown fill model '{other}' (want immediate|partial|reject)"),
        }
    }
}

/// One `ExecutionReport` to build and send, queued from `on_msg_from_app`
/// and dispatched from `run`'s own thread. Plain data only (no `SessionId`
/// or `Message` -- neither is `Send`; a fresh `SessionId` is built once on
/// the sending side instead, see `run`).
struct PendingExec {
    cl_ord_id: String,
    symbol: String,
    side: String,
    order_qty_e2: i64,
    last_qty_e2: i64,
    cum_qty_e2: i64,
    ord_status: &'static str,
    exec_type: &'static str,
}

/// Run the FIX acceptor until the process is killed (Ctrl-C). Blocks the
/// calling thread on the pending-exec channel -- sim's acceptor mode is a
/// standalone demo counterparty process, not something `d1`'s shutdown flag
/// needs to coordinate with.
pub fn run(
    cfg_path: &Path,
    fill_model: FillModel,
    sender_comp_id: &str,
    target_comp_id: &str,
) -> Result<()> {
    let settings = SessionSettings::try_from_path(cfg_path)
        .with_context(|| format!("loading FIX session settings from {}", cfg_path.display()))?;
    let store_factory = MemoryMessageStoreFactory::new();
    let log_factory = LogFactory::try_new(&StdLogger::Stdout).context("FIX log factory")?;

    let (tx, rx) = mpsc::channel::<PendingExec>();
    let callbacks = SimApplication::new(fill_model, tx);
    let app = Application::try_new(&callbacks).context("FIX application")?;
    let mut acceptor = Acceptor::try_new(
        &settings,
        &app,
        &store_factory,
        &log_factory,
        FixSocketServerKind::SingleThreaded,
    )
    .context("FIX acceptor")?;

    println!("sim: FIX acceptor listening (fill_model={fill_model:?})");
    // `start()` already spins up the engine's own connection-handling
    // (matching the crate's own getting-started example: other work happens
    // on this thread *after* start(), not via a separate blocking call --
    // `block()` is an alternative single-threaded mode, not additive on top
    // of start()).
    acceptor.start().context("FIX acceptor start")?;

    // Build our own reply-side SessionId once (this cfg has exactly one
    // static session) and dispatch from here, never from inside
    // `on_msg_from_app` -- calling `send_to_target` re-entrantly, from
    // within the engine's own callback for the message that triggered the
    // reply, was observed to kill the connection (Connection reset by peer).
    let session_id = SessionId::try_new("FIX.4.4", sender_comp_id, target_comp_id, "")
        .context("building reply SessionId")?;
    for pending in rx {
        if let Err(err) = send_exec(&session_id, pending) {
            eprintln!("sim: failed to send exec: {err}");
        }
    }
    Ok(())
}

struct SimApplication {
    fill_model: FillModel,
    tx: mpsc::Sender<PendingExec>,
}

impl SimApplication {
    fn new(fill_model: FillModel, tx: mpsc::Sender<PendingExec>) -> Self {
        Self { fill_model, tx }
    }
}

impl ApplicationCallback for SimApplication {
    fn on_msg_from_app(&self, msg: &Message, _session: &SessionId) -> Result<(), MsgFromAppError> {
        // MsgType (35) lives in the header, not the body -- Message::get_field
        // only reads body fields.
        if msg.with_header(|h| h.get_field(TAG_MSG_TYPE)).as_deref() != Some("D") {
            return Ok(()); // only NewOrderSingle is meaningful here
        }

        let cl_ord_id = msg
            .get_field(TAG_CL_ORD_ID)
            .ok_or(MsgFromAppError::FieldNotFound)?;
        let symbol = msg
            .get_field(TAG_SYMBOL)
            .ok_or(MsgFromAppError::FieldNotFound)?;
        let side = msg
            .get_field(TAG_SIDE)
            .ok_or(MsgFromAppError::FieldNotFound)?;
        let order_qty_str = msg
            .get_field(TAG_ORDER_QTY)
            .ok_or(MsgFromAppError::FieldNotFound)?;
        let order_qty_e2 =
            parse_fixed(&order_qty_str, 2).ok_or(MsgFromAppError::IncorrectDataFormat)?;

        for pending in self.pendings_for(&cl_ord_id, &symbol, &side, order_qty_e2) {
            // Receiver lives on run()'s own thread for the process lifetime;
            // a send error here would only mean that thread is gone, which
            // means the process is shutting down anyway -- nothing to do.
            let _ = self.tx.send(pending);
        }
        Ok(())
    }
}

impl SimApplication {
    fn pendings_for(
        &self,
        cl_ord_id: &str,
        symbol: &str,
        side: &str,
        order_qty_e2: i64,
    ) -> Vec<PendingExec> {
        let base = |last_qty_e2: i64, cum_qty_e2: i64, ord_status, exec_type| PendingExec {
            cl_ord_id: cl_ord_id.to_string(),
            symbol: symbol.to_string(),
            side: side.to_string(),
            order_qty_e2,
            last_qty_e2,
            cum_qty_e2,
            ord_status,
            exec_type,
        };

        match self.fill_model {
            FillModel::ImmediateFull => {
                vec![base(order_qty_e2, order_qty_e2, "2", "2")] // OrdStatus/ExecType: Filled
            }
            FillModel::Partial => {
                let first = order_qty_e2 / 2;
                vec![
                    base(first, first, "1", "1"),                       // PartiallyFilled
                    base(order_qty_e2 - first, order_qty_e2, "2", "2"), // Filled
                ]
            }
            FillModel::Reject => vec![base(0, 0, "8", "8")], // Rejected
        }
    }
}

fn next_exec_id(counter: &AtomicU64) -> String {
    format!("EXEC-{}", counter.fetch_add(1, Ordering::Relaxed))
}

fn send_exec(session_id: &SessionId, pending: PendingExec) -> Result<(), QuickFixError> {
    // ponytail: a fresh counter per call would collide across execs of the
    // same order; this needs to be shared, so route it through a static --
    // simplest option for a single-process demo counterparty, no ADR-worthy
    // dependency needed for a monotonic id.
    static EXEC_SEQ: AtomicU64 = AtomicU64::new(1);

    let mut msg = Message::new();
    msg.with_header_mut(|h| h.set_field(TAG_MSG_TYPE, "8"))?;
    msg.set_field(TAG_ORDER_ID, format!("SIM-{}", pending.cl_ord_id))?;
    msg.set_field(TAG_CL_ORD_ID, pending.cl_ord_id)?;
    msg.set_field(TAG_EXEC_ID, next_exec_id(&EXEC_SEQ))?;
    msg.set_field(TAG_EXEC_TYPE, pending.exec_type)?;
    msg.set_field(TAG_ORD_STATUS, pending.ord_status)?;
    msg.set_field(TAG_SYMBOL, pending.symbol)?;
    msg.set_field(TAG_SIDE, pending.side)?;
    msg.set_field(TAG_ORDER_QTY, fmt_fixed(pending.order_qty_e2, 2))?;
    msg.set_field(TAG_LAST_QTY, fmt_fixed(pending.last_qty_e2, 2))?;
    msg.set_field(TAG_LAST_PX, fmt_fixed(FILL_PX_E9, 9))?;
    msg.set_field(TAG_CUM_QTY, fmt_fixed(pending.cum_qty_e2, 2))?;
    msg.set_field(
        TAG_LEAVES_QTY,
        fmt_fixed(pending.order_qty_e2 - pending.cum_qty_e2, 2),
    )?;

    send_to_target(msg, session_id)
}

/// No floats (root CLAUDE.md's money/quantities invariant) -- see the
/// identical rationale on `d1-gateway-fix::convert::fmt_fixed` (this is
/// sim's own copy, kept private and un-shared per sim/CLAUDE.md's
/// dependency direction rule).
fn fmt_fixed(value_e_n: i64, decimals: u32) -> String {
    let scale = 10i64.pow(decimals);
    let whole = value_e_n / scale;
    let frac = value_e_n % scale;
    format!("{whole}.{frac:0width$}", width = decimals as usize)
}

fn parse_fixed(s: &str, decimals: u32) -> Option<i64> {
    let scale = 10i64.pow(decimals);
    let (whole_str, frac_str) = s.split_once('.').unwrap_or((s, ""));
    let whole: i64 = whole_str.parse().ok()?;

    let mut frac_str = frac_str.to_string();
    frac_str.truncate(decimals as usize);
    while frac_str.len() < decimals as usize {
        frac_str.push('0');
    }
    let frac: i64 = if frac_str.is_empty() {
        0
    } else {
        frac_str.parse().ok()?
    };

    whole.checked_mul(scale)?.checked_add(frac)
}
