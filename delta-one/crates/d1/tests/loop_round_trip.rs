//! P2.M4 Slice 3 proof of Delta One's side of the EXO loop (ADR-014), over a
//! real NATS server and `sim`'s FIX acceptor, through the real `d1::spawn`
//! wiring:
//!
//! 1. `d1.positions.1`: periodic `PositionSnapshot`s (at least 2 inside 2.5 s)
//!    carrying a line for 1001 equal to the startup fill (§7).
//! 2. `d1.marks.1001`: a `MarkPrice` from the feed with `mid_px_e9 > 0`, a
//!    changed mid is republished, and an instrument the feed never ticks
//!    (1002, primed only) has no mark (§2).
//! 3. The crosses round-trip's residual parent produces the book-0 report on
//!    `d1.exec.0.1002` AND an allocation report on `d1.exec.1.1002` with
//!    `last_qty_e2 == 200_000`, the ADR-005 worked example (§5).
//! 4. A `ValuationSnapshot` on `exo.valuations.3` with AAPL vega at the hard
//!    cap yields `RiskLimitAlert{breached}` on `d1.alerts.limits`; a following
//!    snapshot with vega 0 yields `cleared` (§3).
//! 5. No alert for a snapshot without `valuation`, nor for subject book 4 /
//!    payload book 3 (§3 acceptance rules 1 and 2).
//!
//! Requires a NATS server on `127.0.0.1:4222` (`just up`). Marked `#[ignore]`
//! so plain `cargo test`/`just test` stays green without Docker; run with
//! `--ignored`. Also auto-skips at runtime if the server is unreachable.
//! Mirrors `crosses_round_trip.rs`'s setup/skip pattern.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)] // integration test, not hot-path code

use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::{Duration, Instant};

use d1::config::TrackerConfig;
use d1::{FixConfig, SpawnConfig, StartupOrder, spawn};
use d1_core::{BookId, FeedTick, InstrumentId, Side};
use d1_gateway_nats::pb::hedging::common::v1::{InstrumentRef, Meta, ValuationMeta};
use d1_gateway_nats::pb::hedging::live::v1::valuation_snapshot::ProductLine;
use d1_gateway_nats::pb::hedging::live::v1::{
    ExecutionReport, MarkPrice, OrdStatus, PositionSnapshot, RiskLimitAlert, TargetPosition,
    ValuationSnapshot, risk_limit_alert,
};
use futures_util::StreamExt;
use prost::Message;

const NATS_URL: &str = "127.0.0.1:4222";
// Distinct from the other round-trip tests' ports (each `tests/*.rs` is its
// own process, but the OS may still hold a recent run's port).
const FIX_PORT: u16 = 15_040;
const TIMEOUT: Duration = Duration::from_secs(15);

/// Startup order: book 1 / AAPL (1001), the FIX round-trip anchor and the
/// sync point proving the gateway's subscriptions are live.
const STARTUP_CL_ORD_ID: &str = "00000000000000000001";
const STARTUP_QTY_E2: i64 = 100;
const AAPL: u32 = 1001;
/// The cross demo's instrument (MSFT): the feed below never ticks it.
const MSFT: u32 = 1002;
const BOOK1_TARGET_QTY_E2: i64 = 1_000_000;
const BOOK1_BAND_E2: i64 = 1_000_000;
const BOOK2_TARGET_QTY_E2: i64 = -800_000;
const EXPECTED_RESIDUAL_QTY_E2: i64 = 200_000;
/// Seq 1 is the startup order, seq 2 the netting residual.
const RESIDUAL_CL_ORD_ID: &str = "00000000000000000002";

fn meta(msg_id: &str) -> Option<Meta> {
    Some(Meta {
        msg_id: msg_id.to_string(),
        producer: "test".to_string(),
        sent_ns: 1,
        schema_version: 1,
    })
}

fn target(book: u32, qty_e2: i64, band_e2: i64, msg_id: &str) -> TargetPosition {
    TargetPosition {
        meta: meta(msg_id),
        book_id: book,
        instrument: Some(InstrumentRef {
            instrument_id: MSFT,
            ..Default::default()
        }),
        target_qty_e2: qty_e2,
        band_qty_e2: band_e2,
        ..Default::default()
    }
}

/// A `ValuationSnapshot` with one AAPL line. `complete == false` omits the
/// `valuation` block (acceptance rule 2).
fn valuation(
    payload_book: u32,
    as_of_ns: u64,
    vega_e9: i64,
    complete: bool,
    msg_id: &str,
) -> ValuationSnapshot {
    ValuationSnapshot {
        meta: meta(msg_id),
        book_id: payload_book,
        as_of_ns,
        valuation: complete.then(|| ValuationMeta {
            model_id: "heston-test".to_string(),
            params_hash: "abc".to_string(),
            git_sha: "deadbeef".to_string(),
            ..Default::default()
        }),
        lines: vec![ProductLine {
            product_id: "T1".to_string(),
            product_type: "autocallable".to_string(),
            underlying: Some(InstrumentRef {
                instrument_id: AAPL,
                ..Default::default()
            }),
            vega_e9,
            ..Default::default()
        }],
        ..Default::default()
    }
}

/// Next message on `sub` satisfying `pred` after decoding as `M`, panicking
/// on `TIMEOUT`.
async fn await_msg<M: Message + Default>(
    sub: &mut async_nats::Subscriber,
    what: &str,
    pred: impl Fn(&M) -> bool,
) -> M {
    let deadline = Instant::now() + TIMEOUT;
    loop {
        let remaining = deadline
            .checked_duration_since(Instant::now())
            .unwrap_or_default();
        assert!(!remaining.is_zero(), "no {what} within {TIMEOUT:?}");
        match tokio::time::timeout(remaining, sub.next()).await {
            Ok(Some(msg)) => {
                let decoded = M::decode(msg.payload).expect("decode");
                if pred(&decoded) {
                    return decoded;
                }
            }
            Ok(None) => panic!("subscription ended early waiting for {what}"),
            Err(_) => panic!("no {what} within {TIMEOUT:?}"),
        }
    }
}

/// Assert nothing arrives on `sub` for `window`.
async fn assert_silent(sub: &mut async_nats::Subscriber, what: &str, window: Duration) {
    if let Ok(Some(msg)) = tokio::time::timeout(window, sub.next()).await {
        panic!("unexpected {what} on {}: {:?}", msg.subject, msg.payload);
    }
}

struct SimAcceptor {
    child: Child,
}

impl Drop for SimAcceptor {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
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

fn universe_path() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../protocol/refdata/universe.json")
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

fn spawn_sim_acceptor(cfg_path: &Path) -> SimAcceptor {
    let child = Command::new(env!("CARGO"))
        .args([
            "run",
            "--quiet",
            "-p",
            "sim",
            "--",
            "--mode",
            "acceptor",
            "--fill-model",
            "immediate",
            "--cfg",
            cfg_path.to_str().expect("cfg path is valid utf8"),
            "--sender-comp-id",
            &target_comp_id(),
            "--target-comp-id",
            &sender_comp_id(),
        ])
        .current_dir(workspace_root())
        .stdout(Stdio::null())
        .stderr(Stdio::inherit())
        .spawn()
        .expect("spawn sim acceptor");
    SimAcceptor { child }
}

/// AAPL-only feed: a first mid at 100 ms and a changed one at 800 ms
/// (exch_ts_ns counts from feed start). Nothing ever ticks MSFT.
fn aapl_only_feed() -> Vec<FeedTick> {
    let tick = |mid_e9: i64, exch_ts_ns: u64| FeedTick {
        instrument_id: InstrumentId(AAPL),
        bid_px_e9: mid_e9 - 10_000_000,
        ask_px_e9: mid_e9 + 10_000_000,
        last_px_e9: mid_e9,
        exch_ts_ns,
        div_per_share_e9: 0,
    };
    vec![
        tick(150_000_000_000, 100_000_000),
        tick(151_000_000_000, 800_000_000),
    ]
}

#[test]
#[ignore = "requires a NATS server on 127.0.0.1:4222 (`just up`)"]
fn loop_support_round_trip() {
    if !nats_reachable() {
        println!("loop_round_trip: NATS unreachable at {NATS_URL}, skipping (`just up` first)");
        return;
    }

    let tmp = std::env::temp_dir().join(format!("d1-loop-round-trip-{FIX_PORT}"));
    let _ = std::fs::remove_dir_all(&tmp);
    std::fs::create_dir_all(&tmp).expect("create tmp cfg dir");
    let acceptor_cfg_path = write_cfg(&tmp, "acceptor.cfg", &acceptor_cfg());
    let initiator_cfg_path = write_cfg(&tmp, "initiator.cfg", &initiator_cfg());

    let _sim = spawn_sim_acceptor(&acceptor_cfg_path);
    wait_for_port(FIX_PORT, TIMEOUT);

    let universe = d1_refdata::load(&universe_path()).expect("load universe refdata");
    let policy: d1_netting::RefPxPolicy = universe
        .cross_px_policy
        .parse()
        .expect("universe refdata cross_px_policy parses");
    let d1_cfg = d1::config::load(&Path::new(env!("CARGO_MANIFEST_DIR")).join("../../d1.toml"))
        .expect("load d1.toml");
    let vega_hard_e9 = d1_cfg.limits.defaults.vega_hard_e9;

    let shutdown = Arc::new(AtomicBool::new(false));
    let book_ids = universe.book_ids.clone();
    let instrument_ids = universe.instrument_ids.clone();
    let handles = spawn(
        SpawnConfig {
            startup: StartupOrder {
                book: BookId(1),
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
                sampling_interval_s: 60,
                te_window_obs: 250,
                publish_interval_s: 60,
            },
            limits: d1_cfg.limits,
            posttrade: None, // no Kafka broker in this test
            deterministic: false,
            feed_ticks: Some(aapl_only_feed()),
        },
        &shutdown,
    );

    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("build tokio runtime");

    runtime.block_on(async {
        let client = async_nats::connect(NATS_URL).await.expect("connect NATS");
        let mut startup_sub = client.subscribe("d1.exec.1.1001").await.unwrap();
        let mut positions_sub = client.subscribe("d1.positions.1").await.unwrap();
        let mut aapl_marks_sub = client.subscribe("d1.marks.1001").await.unwrap();
        let mut msft_marks_sub = client.subscribe("d1.marks.1002").await.unwrap();
        let mut parent_sub = client.subscribe("d1.exec.0.1002").await.unwrap();
        let mut alloc_sub = client.subscribe("d1.exec.1.1002").await.unwrap();
        let mut alerts_sub = client.subscribe("d1.alerts.limits").await.unwrap();

        // Sync point: the startup order's report proves the gateway is live
        // and the fill is booked.
        let startup: ExecutionReport =
            await_msg(&mut startup_sub, "startup report", |r: &ExecutionReport| {
                r.cl_ord_id == STARTUP_CL_ORD_ID
            })
            .await;
        assert_eq!(startup.status, OrdStatus::Filled as i32);

        // (i) At least 2 snapshots carrying the startup fill inside 2.5 s.
        let window_end = Instant::now() + Duration::from_millis(2_500);
        let mut with_fill = 0;
        while let Some(remaining) = window_end.checked_duration_since(Instant::now()) {
            let Ok(Some(msg)) = tokio::time::timeout(remaining, positions_sub.next()).await else {
                break;
            };
            let snap = PositionSnapshot::decode(msg.payload).unwrap();
            assert_eq!(snap.book_id, 1);
            if snap.lines.iter().any(|l| {
                l.instrument.as_ref().map(|i| i.instrument_id) == Some(AAPL)
                    && l.qty_e2 == STARTUP_QTY_E2
            }) {
                with_fill += 1;
            }
        }
        assert!(
            with_fill >= 2,
            "only {with_fill} position snapshots with the startup fill in 2.5 s"
        );

        // (ii) Marks: a positive mid, then the changed mid.
        let first: MarkPrice = await_msg(&mut aapl_marks_sub, "AAPL mark", |m: &MarkPrice| {
            m.mid_px_e9 > 0
        })
        .await;
        assert!(first.meta.as_ref().unwrap().sent_ns > 0);
        await_msg(
            &mut aapl_marks_sub,
            "AAPL mark at the changed mid",
            |m: &MarkPrice| m.mid_px_e9 == 151_000_000_000,
        )
        .await;

        // (iii) The ADR-005 worked example: parent report on book 0 AND the
        // per-book allocation report on book 1.
        client
            .publish(
                "exo.targets.1.1002",
                target(1, BOOK1_TARGET_QTY_E2, BOOK1_BAND_E2, "loop-b1")
                    .encode_to_vec()
                    .into(),
            )
            .await
            .unwrap();
        client
            .publish(
                "exo.targets.2.1002",
                target(2, BOOK2_TARGET_QTY_E2, 0, "loop-b2")
                    .encode_to_vec()
                    .into(),
            )
            .await
            .unwrap();
        let parent: ExecutionReport = await_msg(
            &mut parent_sub,
            "book-0 parent report",
            |r: &ExecutionReport| r.cl_ord_id == RESIDUAL_CL_ORD_ID,
        )
        .await;
        assert_eq!(parent.book_id, 0);
        assert_eq!(parent.cum_qty_e2, EXPECTED_RESIDUAL_QTY_E2);
        let alloc: ExecutionReport = await_msg(
            &mut alloc_sub,
            "book-1 allocation report",
            |r: &ExecutionReport| r.cl_ord_id == RESIDUAL_CL_ORD_ID,
        )
        .await;
        assert_eq!(alloc.book_id, 1);
        assert_eq!(alloc.last_qty_e2, EXPECTED_RESIDUAL_QTY_E2);
        assert_eq!(
            alloc.exec_id, parent.exec_id,
            "allocation shares the venue exec id"
        );
        assert_eq!(
            alloc.cum_qty_e2, parent.cum_qty_e2,
            "status/cum/leaves are the parent's"
        );

        // (iv) Tier-1 limits: hard breach, then cleared.
        client
            .publish(
                "exo.valuations.3",
                valuation(3, 1, vega_hard_e9, true, "val-1")
                    .encode_to_vec()
                    .into(),
            )
            .await
            .unwrap();
        let is_aapl_vega = |a: &RiskLimitAlert| {
            a.greek == risk_limit_alert::Greek::Vega as i32
                && a.underlying.as_ref().map(|u| u.instrument_id) == Some(AAPL)
        };
        let breach: RiskLimitAlert = await_msg(
            &mut alerts_sub,
            "vega breach alert",
            |a: &RiskLimitAlert| is_aapl_vega(a) && a.breached,
        )
        .await;
        assert_eq!(breach.value_e9, vega_hard_e9);
        assert_eq!(breach.limit_e9, vega_hard_e9);
        assert_eq!((breach.value_qty_e2, breach.limit_qty_e2), (0, 0));
        client
            .publish(
                "exo.valuations.3",
                valuation(3, 2, 0, true, "val-2").encode_to_vec().into(),
            )
            .await
            .unwrap();
        await_msg(
            &mut alerts_sub,
            "vega cleared alert",
            |a: &RiskLimitAlert| is_aapl_vega(a) && a.cleared,
        )
        .await;

        // (v) No alert for: no `valuation` block (book 4); subject book 4
        // with payload book 3 (rule 1).
        client
            .publish(
                "exo.valuations.4",
                valuation(4, 1, vega_hard_e9, false, "val-3")
                    .encode_to_vec()
                    .into(),
            )
            .await
            .unwrap();
        client
            .publish(
                "exo.valuations.4",
                valuation(3, 3, vega_hard_e9, true, "val-4")
                    .encode_to_vec()
                    .into(),
            )
            .await
            .unwrap();
        assert_silent(
            &mut alerts_sub,
            "RiskLimitAlert",
            Duration::from_millis(1_500),
        )
        .await;

        // (ii, cont.) The never-ticked instrument has had no mark all along,
        // across several keepalive intervals.
        assert_silent(
            &mut msft_marks_sub,
            "MarkPrice for a never-ticked instrument",
            Duration::from_millis(100),
        )
        .await;
    });

    shutdown.store(true, Ordering::Relaxed);
    let _ = handles.core.join();
    let _ = handles.feed.join();
    let _ = handles.fix.join();
    let _ = handles.nats.join();
}
