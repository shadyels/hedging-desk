//! P2.M4 Slice 5: the flagship storyline, end to end (ADR-014 §10, docs/ROADMAP.md
//! P2.M4). The REAL EXO service (`uv run python -m exo`) and the real `d1::spawn`
//! wiring run against a real NATS server and `sim`'s FIX acceptor
//! (`--fill-model partial`), driven by `sim/scenarios/tracker-flow.yaml`:
//!
//! 1. EXO values its persisted book (EXO-SP, book 3) and publishes the initial AAPL
//!    hedge; D1 fills it and reports it on `d1.positions.3` (the `exo_book_event`
//!    checkpoint, deadline 10 s).
//! 2. The harness publishes the stand-in tracker flow (D1-CORE target 10,000 AAPL
//!    inside a 10,000 band) at 11 s.
//! 3. AAPL gaps from 187.51 to 176.00 at 12 s; EXO republishes a materially larger
//!    hedge target, D1 crosses it against D1-CORE and sends the residual out over
//!    FIX, with per-book allocation reports that conserve the residual (24 s).
//! 4. A dividend at 25 s and tracker analytics covering it by 30 s.
//!
//! The timeline is NOT parsed here: `sim --mode emit-ticks --script-out` writes the
//! non-feed entries (book event, tracker flow, expects) as a script and the feed ticks
//! as a tick file, so there is a single YAML parser. An `expect`'s `at_ms` is a
//! DEADLINE from D1 start; its trigger is the last preceding non-quote action in
//! script order (ADR-014 §10; heartbeat quotes never trigger: the script carries
//! `action gap` / `action dividend` lines for the feed actions), and it must also be
//! received after the event that satisfied the previous expect.
//!
//! NEVER run this concurrently with other infra tests, `just exo` or `just d1*`: the
//! bus is shared, and in-process D1 consumes every `exo.targets.*`. A pre-flight
//! listens for 1.5 s and refuses to start if any EXO/D1 traffic is already on the bus.
//!
//! Requires a NATS server on `127.0.0.1:4222` (`just up`), `uv` and a synced
//! `exo/.venv`. `#[ignore]`d so plain `cargo test` stays green without Docker; run
//! with `--ignored` (`just storyline`). Auto-skips if NATS is unreachable; a missing
//! `uv` is a FAILURE (the harness cannot run, and silently passing would hide that).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)] // integration test, not hot-path code

use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use d1::config::TrackerConfig;
use d1::{FixConfig, SpawnConfig, StartupOrder, spawn};
use d1_core::{BookId, InstrumentId, Side};
use d1_gateway_nats::pb::hedging::common::v1::{InstrumentRef, Meta, Side as WireSide};
use d1_gateway_nats::pb::hedging::live::v1::{
    ExecutionReport, HealthStatus, InternalCrossNotice, MarkPrice, OrdStatus, PositionSnapshot,
    RiskLimitAlert, TargetPosition, TrackerAnalytics, ValuationSnapshot,
};
use futures_util::StreamExt;
use prost::Message;

const NATS_URL: &str = "127.0.0.1:4222";
// Distinct from the other round-trip tests' ports (each `tests/*.rs` is its own
// process, but the OS may still hold a recent run's port).
const FIX_PORT: u16 = 15_050;
const TIMEOUT: Duration = Duration::from_secs(15);
const EXO_READY_TIMEOUT: Duration = Duration::from_secs(60);
const EXO_STOP_TIMEOUT: Duration = Duration::from_secs(10);
/// How long the pre-flight listens for foreign EXO/D1 traffic.
const QUIET_WINDOW: Duration = Duration::from_millis(1_500);

const D1_CORE: u32 = 1;
const EXO_SP: u32 = 3;
const AAPL: u32 = 1001;
/// D1-CORE's startup position: a BUY of 20,000 AAPL at market (ADR-014 storyline).
const STARTUP_QTY_E2: i64 = 2_000_000;

/// EXO's published-number provenance (`exo/book.toml` seed and n_paths;
/// `exo/src/exo/portfolio/revalue.py::model_id("qe")`).
const EXO_MODEL_ID: &str = "heston-qe-pseudo-antithetic-v1";
const EXO_SEED: u64 = 20_261_024;
const EXO_N_PATHS: u32 = 20_000;

/// Receive times come from independent tasks on one thread, so two messages that
/// became readable in the same poll can be stamped in either order. "Received
/// after X" therefore allows this much skew; the quantity checks (a cross sized from
/// T1, a fresh `cl_ord_id`) are what actually tie an event to its cause.
const SKEW: Duration = Duration::from_millis(20);
/// Polling cadence of the walker, and how long past a deadline it keeps looking
/// before declaring a miss (an event stamped before the deadline is still counted).
const POLL: Duration = Duration::from_millis(20);
const GRACE: Duration = Duration::from_millis(150);

// ---------------------------------------------------------------- recorded traffic

type Stamped<T> = (Instant, T);
/// A probe's hit: the value and the receive time of the event that produced it.
type Found<T> = (T, Instant);

/// Everything the subscribers saw, with receive times.
#[derive(Default)]
struct Log {
    health: Vec<Stamped<HealthStatus>>,
    targets: Vec<Stamped<TargetPosition>>,
    valuations: Vec<Stamped<ValuationSnapshot>>,
    positions: Vec<Stamped<PositionSnapshot>>,
    crosses: Vec<Stamped<InternalCrossNotice>>,
    exec0: Vec<Stamped<ExecutionReport>>,
    exec1: Vec<Stamped<ExecutionReport>>,
    exec3: Vec<Stamped<ExecutionReport>>,
    tracker: Vec<Stamped<TrackerAnalytics>>,
    alerts: Vec<Stamped<RiskLimitAlert>>,
    /// The harness's own D1-CORE `exo.targets.1.1001` stand-in, echoed back by NATS.
    flow_targets: Vec<Stamped<TargetPosition>>,
    /// Payloads that failed to decode (subject: error); checked on every poll.
    decode_errors: Vec<String>,
}

type SharedLog = Arc<Mutex<Log>>;

/// Start of the run (set just before D1 spawns), for [`trace`] timestamps.
static T0: std::sync::OnceLock<Instant> = std::sync::OnceLock::new();

/// Timestamped trace line (ms from D1 start; negative-free: 0 before start).
fn trace(what: &str) {
    let ms = T0.get().map_or(0, |t0| ms_since(*t0, Instant::now()));
    println!("storyline: [{ms:>6} ms] {what}");
}

/// Subscribe to `subject` and push every decoded message, stamped on receipt, with
/// `push`. The task runs whenever the walker awaits.
async fn record<M: Message + Default + 'static>(
    client: &async_nats::Client,
    subject: &str,
    log: &SharedLog,
    push: fn(&mut Log, Stamped<M>),
) {
    let mut sub = client
        .subscribe(subject.to_string())
        .await
        .unwrap_or_else(|e| panic!("subscribe {subject}: {e}"));
    let log = Arc::clone(log);
    tokio::spawn(async move {
        while let Some(msg) = sub.next().await {
            let at = Instant::now();
            let mut log = log.lock().unwrap();
            match M::decode(msg.payload) {
                Ok(decoded) => push(&mut log, (at, decoded)),
                Err(e) => log.decode_errors.push(format!("{}: {e}", msg.subject)),
            }
        }
    });
}

async fn subscribe_all(client: &async_nats::Client, log: &SharedLog) {
    record(client, "exo.health", log, |l, m| l.health.push(m)).await;
    record(
        client,
        "exo.targets.3.1001",
        log,
        |l, m: Stamped<TargetPosition>| {
            trace(&format!(
                "target qty_e2={} band={} as_of_ns={} reason={:?}",
                m.1.target_qty_e2, m.1.band_qty_e2, m.1.as_of_ns, m.1.reason
            ));
            l.targets.push(m)
        },
    )
    .await;
    record(
        client,
        "exo.valuations.3",
        log,
        |l, m: Stamped<ValuationSnapshot>| {
            trace(&format!(
                "valuation as_of_ns={} delta_qty_e2={:?}",
                m.1.as_of_ns,
                m.1.lines.iter().map(|x| x.delta_qty_e2).collect::<Vec<_>>()
            ));
            l.valuations.push(m)
        },
    )
    .await;
    record(client, "d1.marks.1001", log, |_, m: Stamped<MarkPrice>| {
        trace(&format!("mark mid_px_e9={}", m.1.mid_px_e9))
    })
    .await;
    record(client, "d1.positions.3", log, |l, m| l.positions.push(m)).await;
    record(client, "d1.crosses", log, |l, m| l.crosses.push(m)).await;
    record(client, "d1.exec.0.1001", log, |l, m| l.exec0.push(m)).await;
    record(client, "d1.exec.1.1001", log, |l, m| l.exec1.push(m)).await;
    record(client, "d1.exec.3.1001", log, |l, m| l.exec3.push(m)).await;
    record(client, "exo.targets.1.1001", log, |l, m| {
        l.flow_targets.push(m)
    })
    .await;
    record(client, "d1.tracker.1", log, |l, m| l.tracker.push(m)).await;
    // The storyline's limits are far from their warn levels (ADR-014 §8 row 17).
    record(client, "d1.alerts.limits", log, |l, m| l.alerts.push(m)).await;
    // The server must have processed every SUB before D1 can publish anything.
    client.flush().await.expect("flush subscriptions");
}

fn ms_since(t0: Instant, at: Instant) -> u128 {
    at.saturating_duration_since(t0).as_millis()
}

/// Poll `probe` until it yields `(value, event_time)`. The event must have been
/// received by `t0 + deadline_ms`; a miss (nothing, or too late) panics with the
/// expect's name, the deadline and what `probe` reported observing.
async fn wait_for<T>(
    name: &str,
    t0: Instant,
    deadline_ms: u64,
    log: &SharedLog,
    mut probe: impl FnMut(&Log) -> Result<Found<T>, String>,
) -> Found<T> {
    let deadline = t0 + Duration::from_millis(deadline_ms);
    loop {
        let result = {
            let log = log.lock().unwrap();
            assert!(
                log.decode_errors.is_empty(),
                "{name}: undecodable payload(s): {:?}",
                log.decode_errors
            );
            probe(&log)
        };
        match result {
            Ok((value, at)) => {
                println!(
                    "storyline: {name:<26} observed at {:>6} ms, deadline {deadline_ms:>6} ms",
                    ms_since(t0, at)
                );
                assert!(
                    at <= deadline,
                    "{name}: satisfied at {} ms, AFTER its {deadline_ms} ms deadline",
                    ms_since(t0, at)
                );
                return (value, at);
            }
            Err(observed) => assert!(
                Instant::now() <= deadline + GRACE,
                "{name}: not satisfied by its {deadline_ms} ms deadline (now {} ms); observed: {observed}",
                ms_since(t0, Instant::now())
            ),
        }
        tokio::time::sleep(POLL).await;
    }
}

// ------------------------------------------------------------------- script / ticks

#[derive(Debug)]
enum Step {
    BookEvent {
        at_ms: u64,
        book: u32,
    },
    TrackerFlow {
        at_ms: u64,
        book: u32,
        instrument: u32,
        target_qty_e2: i64,
        band_qty_e2: i64,
        note: String,
    },
    /// A non-quote FEED action (`gap`, `dividend`): no work for the harness (the tick
    /// file drives it), but it is an expect trigger and a wall-time reference.
    Action {
        at_ms: u64,
        kind: String,
    },
    Expect {
        at_ms: u64,
        kind: String,
    },
}

/// Parse `sim --script-out` lines (see `crates/sim/src/emit.rs::script_lines`).
fn parse_script(text: &str) -> Vec<Step> {
    text.lines()
        .filter(|l| !l.trim().is_empty())
        .map(|line| {
            // 7 fields at most: a tracker_flow's note is the rest of the line.
            let fields: Vec<&str> = line.splitn(7, ' ').collect();
            let num = |s: &str| -> i64 {
                s.parse()
                    .unwrap_or_else(|_| panic!("bad number in script line '{line}'"))
            };
            match fields.as_slice() {
                [at, "book_event", book] => Step::BookEvent {
                    at_ms: num(at).try_into().unwrap(),
                    book: num(book).try_into().unwrap(),
                },
                [at, "tracker_flow", book, instrument, target, band, note] => Step::TrackerFlow {
                    at_ms: num(at).try_into().unwrap(),
                    book: num(book).try_into().unwrap(),
                    instrument: num(instrument).try_into().unwrap(),
                    target_qty_e2: num(target),
                    band_qty_e2: num(band),
                    note: (*note).to_string(),
                },
                [at, "action", kind] => Step::Action {
                    at_ms: num(at).try_into().unwrap(),
                    kind: (*kind).to_string(),
                },
                [at, "expect", kind] => Step::Expect {
                    at_ms: num(at).try_into().unwrap(),
                    kind: (*kind).to_string(),
                },
                _ => panic!("unparseable script line '{line}'"),
            }
        })
        .collect()
}

/// Scenario time (ms from D1 start) of the script's `action <kind>` line.
fn action_ms(steps: &[Step], wanted: &str) -> u64 {
    steps
        .iter()
        .find_map(|s| match s {
            Step::Action { at_ms, kind } if kind == wanted => Some(*at_ms),
            _ => None,
        })
        .unwrap_or_else(|| panic!("script has no `action {wanted}` line"))
}

// ------------------------------------------------------------------- the probes

/// State accumulated while walking the script.
struct Walk {
    t0: Instant,
    /// Scenario time of the gap / dividend actions.
    gap_ms: u64,
    dividend_ms: u64,
    /// Receive time of the event that satisfied the previous expect.
    prev: Instant,
    /// EXO-SP's initial hedge target (fully filled by the book-event deadline).
    t0_qty: i64,
    /// The stand-in tracker flow: D1-CORE's demand is `STARTUP_QTY_E2 - target`.
    flow_target_e2: i64,
    /// `exo_targets_republished`: the new EXO-SP target and its receive time.
    t1: Option<(i64, Instant)>,
    /// `internal_cross`: receive time.
    cross_at: Option<Instant>,
}

impl Walk {
    fn d1_core_demand_e2(&self) -> i64 {
        STARTUP_QTY_E2 - self.flow_target_e2
    }
}

/// `at` counts as received "after" `bound` (see [`SKEW`]).
fn after(at: Instant, bound: Instant) -> bool {
    at + SKEW >= bound
}

/// The `exo_book_event` checkpoint: a full-provenance valuation, an initial hedge
/// target, the book position equal to it, and no cross yet. Returns `(T0, time)`.
fn probe_book_event(log: &Log) -> Result<Found<i64>, String> {
    let valuation = log.valuations.iter().find(|(_, v)| {
        !v.lines.is_empty()
            && v.valuation.as_ref().is_some_and(|m| {
                m.model_id == EXO_MODEL_ID
                    && m.seed == EXO_SEED
                    && m.n_paths == EXO_N_PATHS
                    && !m.git_sha.is_empty()
            })
    });
    let observed = format!(
        "{} valuations (first meta: {:?}), {} targets, {} position snapshots, {} crosses",
        log.valuations.len(),
        log.valuations.first().map(|(_, v)| &v.valuation),
        log.targets.len(),
        log.positions.len(),
        log.crosses.len()
    );
    let (_, first_target) = log.targets.first().ok_or_else(|| observed.clone())?;
    let t0 = first_target.target_qty_e2;
    if valuation.is_none() || t0 <= 0 {
        return Err(format!("{observed}; T0={t0}"));
    }
    let filled = log
        .positions
        .iter()
        .find(|(_, p)| {
            p.lines.iter().any(|l| {
                l.instrument.as_ref().map(|i| i.instrument_id) == Some(AAPL) && l.qty_e2 == t0
            })
        })
        .ok_or_else(|| format!("{observed}; T0={t0}, no d1.positions.3 line == T0"))?;
    if !log.crosses.is_empty() {
        return Err(format!(
            "{observed}; an InternalCrossNotice arrived before the gap"
        ));
    }
    Ok((t0, filled.0))
}

fn probe_targets_republished(log: &Log, w: &Walk, bound: Instant) -> Result<Found<i64>, String> {
    let hit = log
        .targets
        .iter()
        .find(|(at, tp)| after(*at, bound) && (tp.target_qty_e2 - w.t0_qty).abs() > tp.band_qty_e2);
    if let Some((_, tp)) = hit {
        assert!(
            tp.band_qty_e2 > 0,
            "republished target {} carries band_qty_e2 {} (EXO must publish its no-trade band)",
            tp.target_qty_e2,
            tp.band_qty_e2
        );
        assert!(
            tp.target_qty_e2 > w.t0_qty,
            "T1 {} must exceed T0 {} (the gap raises the hedge; the cross sizing assumes it)",
            tp.target_qty_e2,
            w.t0_qty
        );
    }
    hit.map(|(at, tp)| (tp.target_qty_e2, *at)).ok_or_else(|| {
        let seen: Vec<_> = log
            .targets
            .iter()
            .filter(|(at, _)| after(*at, bound))
            .map(|(_, tp)| (tp.target_qty_e2, tp.band_qty_e2))
            .collect();
        format!(
            "T0={}, targets since the gap (qty_e2, band_qty_e2): {seen:?}",
            w.t0_qty
        )
    })
}

fn probe_cross(log: &Log, w: &Walk, bound: Instant) -> Result<Found<InternalCrossNotice>, String> {
    let (t1, _) = w.t1.expect("t1 set by exo_targets_republished");
    let want = (t1 - w.t0_qty).min(w.d1_core_demand_e2());
    let gap_at = w.t0 + Duration::from_millis(w.gap_ms);
    if let Some((at, c)) = log.crosses.iter().find(|(at, _)| *at + SKEW < gap_at) {
        panic!(
            "InternalCrossNotice at {} ms, BEFORE the gap ({} ms): buy {} / sell {} qty_e2 {}",
            ms_since(w.t0, *at),
            w.gap_ms,
            c.buy_book_id,
            c.sell_book_id,
            c.qty_e2
        );
    }
    log.crosses
        .iter()
        .find(|(at, c)| {
            after(*at, bound)
                && c.buy_book_id == EXO_SP
                && c.sell_book_id == D1_CORE
                && c.instrument.as_ref().map(|i| i.instrument_id) == Some(AAPL)
                && c.qty_e2 == want
        })
        .map(|(at, c)| (c.clone(), *at))
        .ok_or_else(|| {
            let seen: Vec<_> = log
                .crosses
                .iter()
                .map(|(_, c)| (c.buy_book_id, c.sell_book_id, c.qty_e2))
                .collect();
            format!("wanted buy {EXO_SP} / sell {D1_CORE} qty_e2 {want}; crosses seen: {seen:?}")
        })
}

/// The residual external parent order and the conservation of its per-book
/// allocation reports. `before_gap` are the `cl_ord_id`s seen on `d1.exec.0.1001`
/// before the gap (the initial hedge and the startup order's own parents).
fn probe_residual(
    log: &Log,
    w: &Walk,
    bound: Instant,
    before_gap: &[String],
) -> Result<Found<String>, String> {
    let (t1, _) = w.t1.expect("t1 set by exo_targets_republished");
    let residual = (t1 - w.t0_qty) - w.d1_core_demand_e2();
    if residual == 0 {
        return Err("no residual expected: T1-T0 equals D1-CORE's demand exactly".into());
    }
    let (want_side, alloc) = if residual < 0 {
        (WireSide::Sell, &log.exec1)
    } else {
        (WireSide::Buy, &log.exec3)
    };
    let parent = log
        .exec0
        .iter()
        .find(|(at, r)| after(*at, bound) && !before_gap.contains(&r.cl_ord_id))
        .ok_or_else(|| {
            format!(
                "no new parent on d1.exec.0.1001 (seen cl_ord_ids: {:?}); expected residual {residual}",
                log.exec0.iter().map(|(_, r)| &r.cl_ord_id).collect::<Vec<_>>()
            )
        })?;
    let cl_ord_id = &parent.1.cl_ord_id;
    assert_eq!(
        parent.1.side, want_side as i32,
        "residual parent {cl_ord_id} has side {} for residual {residual}",
        parent.1.side
    );
    let want_total = residual.abs();
    let other_alloc = if residual < 0 { &log.exec3 } else { &log.exec1 };
    assert!(
        !other_alloc
            .iter()
            .any(|(_, r)| &r.cl_ord_id == cl_ord_id && r.last_qty_e2 > 0),
        "parent {cl_ord_id} has allocation fills on the book that carries no residual"
    );
    // The parent is pinned to the residual's size and must have finished.
    let parent_reports: Vec<_> = log
        .exec0
        .iter()
        .filter(|(_, r)| &r.cl_ord_id == cl_ord_id)
        .collect();
    let done = parent_reports
        .iter()
        .find(|(_, r)| {
            r.status == OrdStatus::Filled as i32
                && r.cum_qty_e2 == want_total
                && r.leaves_qty_e2 == 0
        })
        .ok_or_else(|| {
            format!(
                "parent {cl_ord_id} not FILLED for |residual| {want_total}; its reports (status, cum, leaves): {:?}",
                parent_reports
                    .iter()
                    .map(|(_, r)| (r.status, r.cum_qty_e2, r.leaves_qty_e2))
                    .collect::<Vec<_>>()
            )
        })?;
    let fills: Vec<&Stamped<ExecutionReport>> = alloc
        .iter()
        .filter(|(_, r)| &r.cl_ord_id == cl_ord_id && r.last_qty_e2 > 0)
        .collect();
    let sum: i64 = fills.iter().map(|(_, r)| r.last_qty_e2).sum();
    if sum != want_total {
        return Err(format!(
            "parent {cl_ord_id} FILLED, but allocation fills sum {sum} != |residual| {want_total}"
        ));
    }
    let last_fill = fills.iter().map(|(at, _)| *at).max().unwrap_or(done.0);
    Ok((cl_ord_id.clone(), last_fill.max(done.0)))
}

fn probe_tracker(log: &Log, w: &Walk, bound: Instant) -> Result<Found<(u32, u64)>, String> {
    let window_floor_ns = w.dividend_ms * 1_000_000;
    log.tracker
        .iter()
        .find(|(at, t)| after(*at, bound) && t.n_obs >= 2 && t.window_end_ns >= window_floor_ns)
        .map(|(at, t)| ((t.n_obs, t.window_end_ns), *at))
        .ok_or_else(|| {
            format!(
                "d1.tracker.1 (n_obs, window_end_ns): {:?}; need n_obs>=2, window_end_ns>={window_floor_ns}",
                log.tracker
                    .iter()
                    .map(|(_, t)| (t.n_obs, t.window_end_ns))
                    .collect::<Vec<_>>()
            )
        })
}

// -------------------------------------------------------------------- processes

/// A child in its OWN process group (`uv run` and `cargo run` are wrappers; the
/// process that matters is their child), signalled as a group so nothing outlives the
/// test. `kill` (no `libc`, no `unsafe`) takes `-<pgid>`; the pgid is the child's pid.
struct Proc {
    child: Child,
}

impl Proc {
    fn spawn(cmd: &mut Command, what: &str) -> Proc {
        use std::os::unix::process::CommandExt;
        let child = cmd
            .process_group(0)
            .spawn()
            .unwrap_or_else(|e| panic!("spawn {what}: {e}"));
        Proc { child }
    }

    fn signal_group(&self, sig: &str) -> bool {
        Command::new("kill")
            .args([sig, "--", &format!("-{}", self.child.id())])
            .stderr(Stdio::null())
            .status()
            .is_ok_and(|s| s.success())
    }
}

impl Drop for Proc {
    fn drop(&mut self) {
        // A child already reaped (by `stop_exo`, say) has freed its pid for reuse, so
        // the group must not be signalled by that number any more.
        if matches!(self.child.try_wait(), Ok(None)) {
            self.signal_group("-KILL");
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
    }
}

fn workspace_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("crates dir")
        .parent()
        .expect("delta-one dir")
        .to_path_buf()
}

fn repo_root() -> PathBuf {
    workspace_root().parent().expect("repo root").to_path_buf()
}

fn universe_path() -> PathBuf {
    repo_root().join("protocol/refdata/universe.json")
}

fn write_cfg(dir: &Path, filename: &str, contents: &str) -> PathBuf {
    let path = dir.join(filename);
    std::fs::write(&path, contents).expect("write cfg");
    path
}

fn sender_comp_id() -> String {
    format!("D1-{FIX_PORT}")
}

fn target_comp_id() -> String {
    format!("SIM-{FIX_PORT}")
}

fn acceptor_cfg() -> String {
    let sender = target_comp_id();
    let target = sender_comp_id();
    format!(
        "[DEFAULT]\nConnectionType=acceptor\nHeartBtInt=30\nUseDataDictionary=N\nResetOnLogon=Y\nStartTime=00:00:00\nEndTime=23:59:59\n\n[SESSION]\nBeginString=FIX.4.4\nSenderCompID={sender}\nTargetCompID={target}\nSocketAcceptPort={FIX_PORT}\n"
    )
}

fn initiator_cfg() -> String {
    let sender = sender_comp_id();
    let target = target_comp_id();
    format!(
        "[DEFAULT]\nConnectionType=initiator\nReconnectInterval=1\nHeartBtInt=30\nUseDataDictionary=N\nResetOnLogon=Y\nStartTime=00:00:00\nEndTime=23:59:59\n\n[SESSION]\nBeginString=FIX.4.4\nSenderCompID={sender}\nTargetCompID={target}\nSocketConnectHost=127.0.0.1\nSocketConnectPort={FIX_PORT}\n"
    )
}

fn wait_for_port(port: u16, timeout: Duration) {
    let deadline = Instant::now() + timeout;
    loop {
        if TcpStream::connect(("127.0.0.1", port)).is_ok() {
            return;
        }
        assert!(
            Instant::now() < deadline,
            "port {port} never opened within {timeout:?}"
        );
        thread::sleep(Duration::from_millis(50));
    }
}

fn nats_reachable() -> bool {
    TcpStream::connect_timeout(
        &NATS_URL.parse().expect("NATS_URL is a valid socket addr"),
        Duration::from_millis(300),
    )
    .is_ok()
}

fn spawn_sim_acceptor(cfg_path: &Path) -> Proc {
    Proc::spawn(
        Command::new(env!("CARGO"))
            .args([
                "run",
                "--quiet",
                "-p",
                "sim",
                "--",
                "--mode",
                "acceptor",
                "--fill-model",
                "partial",
                "--cfg",
                cfg_path.to_str().expect("cfg path is valid utf8"),
                "--sender-comp-id",
                &target_comp_id(),
                "--target-comp-id",
                &sender_comp_id(),
            ])
            .current_dir(workspace_root())
            .stdout(Stdio::null())
            .stderr(Stdio::inherit()),
        "sim acceptor",
    )
}

/// Step 1: expand the scenario into the tick file and the harness script.
fn emit_scenario(tick_path: &Path, script_path: &Path) {
    let status = Command::new(env!("CARGO"))
        .args([
            "run",
            "--quiet",
            "-p",
            "sim",
            "--",
            "--mode",
            "emit-ticks",
            "--scenario",
            "../sim/scenarios/tracker-flow.yaml",
            "--out",
        ])
        .arg(tick_path)
        .arg("--script-out")
        .arg(script_path)
        .current_dir(workspace_root())
        .stdout(Stdio::null())
        .stderr(Stdio::inherit())
        .status()
        .expect("run sim --mode emit-ticks");
    assert!(
        status.success(),
        "sim --mode emit-ticks exited with {status:?}"
    );
}

/// Step 3: a copy of `exo/exo.toml` with `allow_dirty = true` (a dev tree is dirty;
/// EXO's relative paths resolve against its project root, so the copy can live in
/// the tmp dir). Exactly one replacement, or the config changed under us.
fn write_exo_config(tmp: &Path) -> PathBuf {
    const FROM: &str = "allow_dirty = false";
    let text = std::fs::read_to_string(repo_root().join("exo/exo.toml")).expect("read exo.toml");
    assert_eq!(
        text.matches(FROM).count(),
        1,
        "exo/exo.toml must contain `{FROM}` exactly once"
    );
    write_cfg(
        tmp,
        "exo.toml",
        &text.replacen(FROM, "allow_dirty = true", 1),
    )
}

/// Step 4: start EXO. A missing `uv` is a failure, not a skip.
fn spawn_exo(cfg: &Path) -> Proc {
    let uv = Command::new("uv").arg("--version").output();
    assert!(
        matches!(&uv, Ok(out) if out.status.success()),
        "the storyline needs `uv` on PATH to run EXO (https://docs.astral.sh/uv/) and a synced exo/.venv (`cd exo && uv sync`): {uv:?}"
    );
    Proc::spawn(
        Command::new("uv")
            .args(["run", "python", "-m", "exo", "--config"])
            .arg(cfg)
            .current_dir(repo_root().join("exo"))
            .stdout(Stdio::null())
            .stderr(Stdio::inherit()),
        "`uv run python -m exo`",
    )
}

/// SIGTERM EXO's process group and wait for the wrapper to exit, killing it after
/// [`EXO_STOP_TIMEOUT`]. Returns whether it exited on its own.
fn stop_exo(exo: &mut Proc) -> bool {
    let signalled = exo.signal_group("-TERM");
    let deadline = Instant::now() + EXO_STOP_TIMEOUT;
    while Instant::now() < deadline {
        if matches!(exo.child.try_wait(), Ok(Some(_))) {
            return signalled;
        }
        thread::sleep(Duration::from_millis(100));
    }
    false // Proc::drop kills it
}

// -------------------------------------------------------------------------- test
// The tracker_flow stand-in publisher and its unit test come first, then the e2e test.

// ponytail: harness-published D1-CORE TargetPosition is a stand-in for an index-replication source (ADR-014 §10); upgrade: D1's own index-tracking target generator; owner undecided; trigger: the first tracker rebalance requirement
fn tracker_flow_message(
    book: u32,
    instrument: u32,
    target_qty_e2: i64,
    band_qty_e2: i64,
    note: &str,
) -> TargetPosition {
    TargetPosition {
        meta: Some(Meta {
            msg_id: uuid::Uuid::now_v7().to_string(),
            producer: "sim".to_string(),
            sent_ns: now_ns(),
            schema_version: 1,
        }),
        book_id: book,
        instrument: Some(InstrumentRef {
            instrument_id: instrument,
            ..Default::default()
        }),
        target_qty_e2,
        band_qty_e2,
        valuation: None,
        reason: format!("tracker_flow: {note}"), // ADR-014 §10
        ..Default::default()
    }
}

async fn publish_tracker_flow(client: &async_nats::Client, flow: &TargetPosition) {
    let instrument = flow.instrument.as_ref().map_or(0, |i| i.instrument_id);
    client
        .publish(
            format!("exo.targets.{}.{instrument}", flow.book_id),
            flow.encode_to_vec().into(),
        )
        .await
        .expect("publish tracker_flow TargetPosition");
    client.flush().await.expect("flush tracker_flow");
}

fn now_ns() -> u64 {
    u64::try_from(
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos(),
    )
    .unwrap()
}

/// ADR-014 §10: the stand-in carries `producer="sim"`, no `valuation`, and
/// `reason="tracker_flow: <note>"`, with the note taken verbatim from the script line.
#[test]
fn tracker_flow_message_follows_adr_014_s10() {
    let steps = parse_script(
        "11000 tracker_flow 1 1001 1000000 1000000 index redemption: sell 10,000 AAPL\n",
    );
    let [
        Step::TrackerFlow {
            book,
            instrument,
            target_qty_e2,
            band_qty_e2,
            note,
            ..
        },
    ] = steps.as_slice()
    else {
        panic!("expected one tracker_flow step, got {steps:?}");
    };
    let msg = tracker_flow_message(*book, *instrument, *target_qty_e2, *band_qty_e2, note);
    assert_eq!(
        msg.reason,
        "tracker_flow: index redemption: sell 10,000 AAPL"
    );
    assert_eq!(msg.meta.as_ref().map(|m| m.producer.as_str()), Some("sim"));
    assert!(msg.valuation.is_none());
    assert_eq!(
        (msg.book_id, msg.target_qty_e2, msg.band_qty_e2),
        (1, 1_000_000, 1_000_000)
    );
}

#[test]
#[ignore = "requires NATS on 127.0.0.1:4222 (`just up`), `uv` and a synced exo/.venv"]
fn storyline_tracker_flow() {
    if !nats_reachable() {
        println!("storyline: NATS unreachable at {NATS_URL}, skipping (`just up` first)");
        return;
    }

    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("build tokio runtime");
    let log: SharedLog = Arc::default();
    let client = runtime.block_on(async {
        let client = async_nats::connect(NATS_URL).await.expect("connect NATS");
        // Pre-flight, before anything is spawned: the bus must be quiet. In-process D1
        // consumes every `exo.targets.*`, so foreign traffic cannot be filtered out.
        let mut watch = Vec::new();
        for subject in ["exo.health", "exo.targets.>", "d1.positions.>", "d1.marks.>"] {
            watch.push((
                subject,
                client.subscribe(subject).await.expect("subscribe pre-flight"),
            ));
        }
        client.flush().await.expect("flush pre-flight");
        tokio::time::sleep(QUIET_WINDOW).await;
        for (subject, sub) in &mut watch {
            use futures_util::FutureExt;
            if let Some(Some(msg)) = sub.next().now_or_never() {
                panic!(
                    "bus not quiet: {} arrived on {subject} -- another EXO/D1 (or an infra test) is running",
                    msg.subject
                );
            }
        }
        // 5. Subscribe BEFORE anything can publish (health first: step 4 reads it).
        subscribe_all(&client, &log).await;
        client
    });

    // A fresh directory only we can have created: `create_dir` fails if it exists, so a
    // planted directory or symlink on a shared /tmp cannot feed us configs.
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let tmp = std::env::temp_dir().join(format!("d1-storyline-{}-{nanos}", std::process::id()));
    std::fs::create_dir(&tmp).expect("create fresh tmp dir");

    // 1. Tick file + script from the one YAML parser.
    let (tick_path, script_path) = (tmp.join("t.ticks"), tmp.join("t.script"));
    emit_scenario(&tick_path, &script_path);
    let ticks = d1::tickfile::load(&tick_path).expect("load emitted tick file");
    let steps = parse_script(&std::fs::read_to_string(&script_path).expect("read script"));
    let (gap_ms, dividend_ms) = (action_ms(&steps, "gap"), action_ms(&steps, "dividend"));

    // 2. The FIX acceptor, partial fills.
    let acceptor_cfg_path = write_cfg(&tmp, "acceptor.cfg", &acceptor_cfg());
    let initiator_cfg_path = write_cfg(&tmp, "initiator.cfg", &initiator_cfg());
    let _sim = spawn_sim_acceptor(&acceptor_cfg_path);
    wait_for_port(FIX_PORT, TIMEOUT);

    // 3 + 4. EXO on a dirty-allowed copy of its config; wait until it can publish.
    let mut exo = spawn_exo(&write_exo_config(&tmp));
    // Async sleeps, not `thread::sleep`: the subscriber tasks (and the NATS connection)
    // only make progress while the runtime is being driven.
    runtime.block_on(async {
        let ready_deadline = Instant::now() + EXO_READY_TIMEOUT;
        loop {
            let ready = log.lock().unwrap().health.iter().any(|(_, h)| {
                h.ok && h.detail.get("publish").map(String::as_str) == Some("enabled")
            });
            if ready {
                break;
            }
            if let Ok(Some(status)) = exo.child.try_wait() {
                panic!("EXO exited during startup with {status}");
            }
            assert!(
                Instant::now() < ready_deadline,
                "no exo.health with ok=true and publish=enabled within {EXO_READY_TIMEOUT:?}; last: {:?}",
                log.lock().unwrap().health.last().map(|(_, h)| (h.ok, h.detail.clone()))
            );
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    });

    // 6. Delta One: the real `spawn`, the scenario's ticks, 1 s tracker sampling.
    let universe = d1_refdata::load(&universe_path()).expect("load universe refdata");
    let policy: d1_netting::RefPxPolicy = universe
        .cross_px_policy
        .parse()
        .expect("universe refdata cross_px_policy parses");
    let d1_cfg = d1::config::load(&workspace_root().join("d1.toml")).expect("load d1.toml");
    let shutdown = Arc::new(AtomicBool::new(false));
    let book_ids = universe.book_ids.clone();
    let instrument_ids = universe.instrument_ids.clone();
    let t0 = Instant::now();
    let _ = T0.set(t0);
    let handles = spawn(
        SpawnConfig {
            startup: StartupOrder {
                book: BookId(D1_CORE),
                instrument: InstrumentId(AAPL),
                side: Side::Buy,
                qty_e2: STARTUP_QTY_E2,
                px_e9: 0,
            },
            fix: FixConfig {
                settings_path: initiator_cfg_path,
                sender_comp_id: sender_comp_id(),
                target_comp_id: target_comp_id(),
            },
            nats_url: NATS_URL.to_string(),
            book_ids,
            instrument_ids,
            policy,
            universe,
            tracker: TrackerConfig {
                sampling_interval_s: 1,
                te_window_obs: 250,
                publish_interval_s: 1,
            },
            limits: d1_cfg.limits,
            posttrade: None, // NATS only; no Kafka in this test
            deterministic: false,
            feed_ticks: Some(ticks),
        },
        &shutdown,
    );

    // 7. Walk the script.
    let mut walk = Walk {
        t0,
        gap_ms,
        dividend_ms,
        prev: t0,
        t0_qty: 0,
        flow_target_e2: 0,
        t1: None,
        cross_at: None,
    };
    let mut before_gap: Vec<String> = Vec::new();
    // Trigger of the next expect: the last non-quote action seen in script order.
    let mut trigger_ms: u64 = 0;

    runtime.block_on(async {
        for step in &steps {
            match step {
                Step::BookEvent { at_ms, book } => {
                    trigger_ms = *at_ms;
                    assert_eq!(*book, EXO_SP, "the storyline checkpoints EXO-SP");
                    let (t0_qty, at) =
                        wait_for("book_event", t0, *at_ms, &log, probe_book_event).await;
                    walk.t0_qty = t0_qty;
                    walk.prev = at;
                }
                Step::TrackerFlow {
                    at_ms,
                    book,
                    instrument,
                    target_qty_e2,
                    band_qty_e2,
                    note,
                } => {
                    trigger_ms = *at_ms;
                    tokio::time::sleep_until(tokio::time::Instant::from_std(
                        t0 + Duration::from_millis(*at_ms),
                    ))
                    .await;
                    assert_eq!(*book, D1_CORE, "the stand-in flow is D1-CORE's");
                    walk.flow_target_e2 = *target_qty_e2;
                    assert!(
                        walk.d1_core_demand_e2() > 0,
                        "tracker_flow must SELL down from the {STARTUP_QTY_E2} startup position"
                    );
                    let flow = tracker_flow_message(
                        *book,
                        *instrument,
                        *target_qty_e2,
                        *band_qty_e2,
                        note,
                    );
                    publish_tracker_flow(&client, &flow).await;
                    println!(
                        "storyline: tracker_flow published at {} ms",
                        ms_since(t0, Instant::now())
                    );
                    // The message must come back off the bus intact (reason included).
                    let (reason, _) =
                        wait_for("tracker_flow_echo", t0, *at_ms + 2_000, &log, |l| {
                            l.flow_targets
                                .iter()
                                .find(|(_, tp)| tp.reason == flow.reason)
                                .map(|(at, tp)| (tp.reason.clone(), *at))
                                .ok_or_else(|| {
                                    format!("{} flow targets seen", l.flow_targets.len())
                                })
                        })
                        .await;
                    println!("storyline: tracker_flow reason observed: {reason:?}");
                }
                Step::Action { at_ms, .. } => trigger_ms = *at_ms,
                Step::Expect { at_ms, kind } => {
                    let bound = (t0 + Duration::from_millis(trigger_ms)).max(walk.prev);
                    match kind.as_str() {
                        "exo_targets_republished" => {
                            let (t1, at) = wait_for(kind, t0, *at_ms, &log, |l| {
                                probe_targets_republished(l, &walk, bound)
                            })
                            .await;
                            walk.t1 = Some((t1, at));
                            walk.prev = at;
                        }
                        "internal_cross" => {
                            let (cross, at) =
                                wait_for(kind, t0, *at_ms, &log, |l| probe_cross(l, &walk, bound))
                                    .await;
                            assert!(!cross.cross_id.is_empty());
                            println!(
                                "storyline: T0={} T1={} cross qty_e2={}",
                                walk.t0_qty,
                                walk.t1.map_or(0, |t| t.0),
                                cross.qty_e2
                            );
                            walk.cross_at = Some(at);
                            walk.prev = at;
                        }
                        "external_residual_order" => {
                            // `cl_ord_id`s seen before the gap are not the residual.
                            before_gap = log
                                .lock()
                                .unwrap()
                                .exec0
                                .iter()
                                .filter(|(at, _)| *at < t0 + Duration::from_millis(walk.gap_ms))
                                .map(|(_, r)| r.cl_ord_id.clone())
                                .collect();
                            let (cl_ord_id, at) = wait_for(kind, t0, *at_ms, &log, |l| {
                                probe_residual(l, &walk, bound, &before_gap)
                            })
                            .await;
                            let (t1, _) = walk.t1.unwrap();
                            let residual = (t1 - walk.t0_qty) - walk.d1_core_demand_e2();
                            println!(
                                "storyline: residual parent {cl_ord_id} {} qty_e2={}",
                                if residual < 0 { "SELL" } else { "BUY" },
                                residual.abs()
                            );
                            walk.prev = at;
                        }
                        "tracker_analytics" => {
                            let ((n_obs, window_end_ns), at) =
                                wait_for(kind, t0, *at_ms, &log, |l| {
                                    probe_tracker(l, &walk, bound)
                                })
                                .await;
                            println!(
                                "storyline: tracker n_obs={n_obs} window_end_ns={window_end_ns}"
                            );
                            walk.prev = at;
                        }
                        other => panic!("expect '{other}' is not implemented before P4"),
                    }
                }
            }
        }
    });
    assert!(
        walk.cross_at.is_some() && walk.t1.is_some(),
        "storyline never reached its cross"
    );
    assert!(
        log.lock().unwrap().alerts.is_empty(),
        "unexpected RiskLimitAlert(s) during the storyline: {:?}",
        log.lock()
            .unwrap()
            .alerts
            .iter()
            .map(|(_, a)| a)
            .collect::<Vec<_>>()
    );

    // 8. Shut down: SIGTERM EXO first (D1's 15 s stale-valuation WARN cannot fire
    // inside the at most 10 s window), then D1 in the ordered sequence; the
    // acceptor goes with `_sim`'s Drop.
    let exo_exited_cleanly = stop_exo(&mut exo);
    shutdown.store(true, Ordering::Relaxed);
    // A panic or an error return on a D1 thread must fail the test.
    handles.core.join().expect("D1 core thread panicked");
    handles.feed.join().expect("D1 feed thread panicked");
    handles
        .fix
        .join()
        .expect("D1 FIX thread panicked")
        .expect("D1 FIX thread errored");
    handles
        .nats
        .join()
        .expect("D1 NATS thread panicked")
        .expect("D1 NATS thread errored");
    assert!(
        log.lock().unwrap().decode_errors.is_empty(),
        "undecodable payload(s): {:?}",
        log.lock().unwrap().decode_errors
    );
    assert!(
        exo_exited_cleanly,
        "EXO did not exit within {EXO_STOP_TIMEOUT:?} of SIGTERM"
    );
    let _ = std::fs::remove_dir_all(&tmp);
}
