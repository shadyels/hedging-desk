//! Slice 3 DoD proof: publish a `TargetPosition` on `exo.targets.<book>.<instrument>`
//! (as EXO would), drive it through the real `d1::spawn` wiring --
//! `d1-gateway-nats` consume -> `target_to_order` -> FIX `NewOrderSingle` ->
//! a real `sim` acceptor -> `ExecutionReport` -> `d1-gateway-nats` publish --
//! and assert an `ExecutionReport` reaching `Filled` arrives on
//! `d1.exec.<book>.<instrument>`. Mirrors `fix_round_trip.rs`'s shape (real
//! subprocess, real sockets) plus the live NATS plane this slice adds.
//!
//! Requires a NATS server on `127.0.0.1:4222` (`just up`). Marked `#[ignore]`
//! so plain `cargo test`/`just test` stays green without Docker; run
//! explicitly with `--ignored`. Also auto-skips at runtime if the server
//! turns out to be unreachable, so an accidental `--ignored` run in an
//! environment without NATS up fails soft instead of hanging.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)] // integration test, not hot-path code

use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::{Duration, Instant};

use d1::{FixConfig, StartupOrder, spawn};
use d1_core::{BookId, InstrumentId, Side};
use d1_gateway_nats::pb::hedging::common::v1::{InstrumentRef, Meta};
use d1_gateway_nats::pb::hedging::live::v1::{ExecutionReport, OrdStatus, TargetPosition};
use futures_util::StreamExt;
use prost::Message;

const NATS_URL: &str = "127.0.0.1:4222";
const FIX_PORT: u16 = 15_028;
const ROUND_TRIP_TIMEOUT: Duration = Duration::from_secs(15);
/// How long to wait for a (forbidden) second report after a redelivery.
const DUP_QUIET_WINDOW: Duration = Duration::from_secs(3);
// `d1::run_core` places the CLI startup order as ClOrdId seq 1, then hands
// out seq 2 to the first NATS-driven target -- see lib.rs's `next_seq`.
const STARTUP_CL_ORD_ID: &str = "00000000000000000001";
const TARGET_DRIVEN_CL_ORD_ID: &str = "00000000000000000002";
/// The next order after the startup + book-1 target, i.e. the book-2 target
/// below (see `d1::run_core`'s `next_seq`).
const BOOK2_TARGET_CL_ORD_ID: &str = "00000000000000000003";
/// Startup order size; establishes a position the target must net against.
const STARTUP_QTY_E2: i64 = 100;
/// Absolute target position EXO asks for, well above `STARTUP_QTY_E2`.
const TARGET_QTY_E2: i64 = 5_000;
/// Absolute target position for book 2 / instrument 2001 -- in-universe, no
/// prior position, so demand = this value minus zero.
const BOOK2_TARGET_QTY_E2: i64 = 3_000;

/// Read from `subscriber` until a report for `cl_ord_id` arrives, panicking
/// on `ROUND_TRIP_TIMEOUT`. Reports for other orders are skipped, not failed.
async fn await_report(subscriber: &mut async_nats::Subscriber, cl_ord_id: &str) -> ExecutionReport {
    let deadline = Instant::now() + ROUND_TRIP_TIMEOUT;
    loop {
        let remaining = deadline
            .checked_duration_since(Instant::now())
            .unwrap_or_default();
        assert!(
            !remaining.is_zero(),
            "no ExecutionReport for cl_ord_id={cl_ord_id} within {ROUND_TRIP_TIMEOUT:?}"
        );
        match tokio::time::timeout(remaining, subscriber.next()).await {
            Ok(Some(msg)) => {
                let report = ExecutionReport::decode(msg.payload).expect("decode ExecutionReport");
                if report.cl_ord_id == cl_ord_id {
                    return report;
                }
            }
            Ok(None) => panic!("subscription ended early"),
            Err(_) => {
                panic!("no ExecutionReport for cl_ord_id={cl_ord_id} within {ROUND_TRIP_TIMEOUT:?}")
            }
        }
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

/// Same path shape as `d1-refdata`'s own unit test: from `crates/d1` up to
/// the repo root, then into `protocol/refdata/universe.json`.
fn universe_path() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../protocol/refdata/universe.json")
}

fn write_cfg(dir: &Path, filename: &str, contents: &str) -> PathBuf {
    let path = dir.join(filename);
    std::fs::write(&path, contents).expect("write cfg");
    path
}

// See fix_round_trip.rs: quickfix's SessionID registry is process-global, so
// this test's comp ids must not collide with any other FIX test running in
// the same `cargo test` process.
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

#[test]
#[ignore = "requires a NATS server on 127.0.0.1:4222 (`just up`)"]
fn target_position_round_trips_to_execution_report() {
    if !nats_reachable() {
        println!("nats_round_trip: NATS unreachable at {NATS_URL}, skipping (`just up` first)");
        return;
    }

    let tmp = std::env::temp_dir().join(format!("d1-nats-round-trip-{FIX_PORT}"));
    let _ = std::fs::remove_dir_all(&tmp);
    std::fs::create_dir_all(&tmp).expect("create tmp cfg dir");
    let acceptor_cfg_path = write_cfg(&tmp, "acceptor.cfg", &acceptor_cfg());
    let initiator_cfg_path = write_cfg(&tmp, "initiator.cfg", &initiator_cfg());

    let _sim = spawn_sim_acceptor(&acceptor_cfg_path);
    wait_for_port(FIX_PORT, ROUND_TRIP_TIMEOUT);

    let universe = d1_refdata::load(&universe_path()).expect("load universe refdata");

    let shutdown = Arc::new(AtomicBool::new(false));
    let handles = spawn(
        StartupOrder {
            book: BookId(1),
            instrument: InstrumentId(1001),
            side: Side::Buy,
            qty_e2: STARTUP_QTY_E2,
            px_e9: 0,
        },
        FixConfig {
            settings_path: initiator_cfg_path,
            sender_comp_id: sender_comp_id(),
            target_comp_id: target_comp_id(),
        },
        NATS_URL.to_string(),
        universe.book_ids,
        universe.instrument_ids,
        &shutdown,
    );

    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("build tokio runtime");

    let report = runtime.block_on(async {
        let client = async_nats::connect(NATS_URL).await.expect("connect NATS");
        let mut subscriber = client
            .subscribe("d1.exec.1.1001")
            .await
            .expect("subscribe d1.exec.1.1001");
        // Book 2 / instrument 2001 is in-universe (protocol/refdata/universe.json)
        // but is not the CLI startup pair -- the keeper/market-data universe
        // swap (P1.M3 slice 1) is what makes an order here bookable at all.
        let mut book2_subscriber = client
            .subscribe("d1.exec.2.2001")
            .await
            .expect("subscribe d1.exec.2.2001");
        // book 99 / instrument 999999 are in no book/instrument index in
        // universe.json: the keeper returns `None` for them, so the
        // wildcard-target guard rejects -- a position with nowhere to book
        // (root CLAUDE.md #2). This subject must never see a report.
        let mut off_universe = client
            .subscribe("d1.exec.99.999999")
            .await
            .expect("subscribe d1.exec.99.999999");

        // Wait for the startup order's own report before publishing the
        // target. Core NATS doesn't queue for a subscriber that isn't
        // registered yet, and `d1::spawn`'s gateway needs a moment to connect
        // + subscribe -- but `run_gateway_async` subscribes to `exo.targets.>`
        // *before* it ever publishes an exec report, so seeing seq 1's report
        // proves the target subscription is already live. It also proves the
        // startup fill is booked (the core books the fill before pushing the
        // report), which is what makes the target-driven quantity below
        // deterministic rather than a race.
        let startup = await_report(&mut subscriber, STARTUP_CL_ORD_ID).await;
        assert_eq!(startup.status, OrdStatus::Filled as i32);
        assert_eq!(startup.cum_qty_e2, STARTUP_QTY_E2);

        let target = TargetPosition {
            meta: Some(Meta {
                msg_id: "test-target-1".to_string(),
                producer: "test".to_string(),
                sent_ns: 1,
                schema_version: 1,
            }),
            book_id: 1,
            instrument: Some(InstrumentRef {
                instrument_id: 1001,
                ..Default::default()
            }),
            target_qty_e2: TARGET_QTY_E2, // absolute desired position, not a delta
            ..Default::default()
        };
        let payload = target.encode_to_vec();
        client
            .publish("exo.targets.1.1001", payload.clone().into())
            .await
            .expect("publish TargetPosition");

        let report = await_report(&mut subscriber, TARGET_DRIVEN_CL_ORD_ID).await;

        // Redelivery is explicitly allowed (root CLAUDE.md #4), so the same
        // msg_id going out twice must not place a second order. Nothing more
        // may arrive on the exec subject after this.
        client
            .publish("exo.targets.1.1001", payload.into())
            .await
            .expect("republish TargetPosition");
        if let Ok(Some(msg)) = tokio::time::timeout(DUP_QUIET_WINDOW, subscriber.next()).await {
            let extra = ExecutionReport::decode(msg.payload).expect("decode ExecutionReport");
            panic!(
                "redelivered TargetPosition produced a second order: cl_ord_id={} cum_qty_e2={}",
                extra.cl_ord_id, extra.cum_qty_e2
            );
        }

        // Book 2 / instrument 2001: in-universe, not the startup pair, no
        // prior position. The universe swap means this now gets a keeper
        // slot and the target is ACCEPTED -- the headline proof this slice's
        // universe wiring works.
        let book2_target = TargetPosition {
            meta: Some(Meta {
                msg_id: "test-target-book2".to_string(),
                producer: "test".to_string(),
                sent_ns: 1,
                schema_version: 1,
            }),
            book_id: 2,
            instrument: Some(InstrumentRef {
                instrument_id: 2001,
                ..Default::default()
            }),
            target_qty_e2: BOOK2_TARGET_QTY_E2, // absolute; no prior position, so demand = this value
            ..Default::default()
        };
        client
            .publish("exo.targets.2.2001", book2_target.encode_to_vec().into())
            .await
            .expect("publish book-2 TargetPosition");
        let book2_report = await_report(&mut book2_subscriber, BOOK2_TARGET_CL_ORD_ID).await;
        assert_eq!(book2_report.status, OrdStatus::Filled as i32);
        assert_eq!(book2_report.book_id, 2);
        assert_eq!(
            book2_report.instrument.as_ref().unwrap().instrument_id,
            2001
        );
        assert_eq!(book2_report.cum_qty_e2, BOOK2_TARGET_QTY_E2);
        assert_eq!(book2_report.leaves_qty_e2, 0);

        // A target naming a book/instrument genuinely outside universe.json
        // (no book/instrument index entry at all) must still be rejected
        // outright, not traded and then silently left unbooked.
        let stray = TargetPosition {
            meta: Some(Meta {
                msg_id: "test-target-off-universe".to_string(),
                producer: "test".to_string(),
                sent_ns: 1,
                schema_version: 1,
            }),
            book_id: 99,
            instrument: Some(InstrumentRef {
                instrument_id: 999_999,
                ..Default::default()
            }),
            target_qty_e2: 7_000,
            ..Default::default()
        };
        client
            .publish("exo.targets.99.999999", stray.encode_to_vec().into())
            .await
            .expect("publish off-universe TargetPosition");
        if let Ok(Some(msg)) = tokio::time::timeout(DUP_QUIET_WINDOW, off_universe.next()).await {
            let extra = ExecutionReport::decode(msg.payload).expect("decode ExecutionReport");
            panic!(
                "target for unconfigured book/instrument was traded anyway: cl_ord_id={} book={} cum_qty_e2={} -- that position has nowhere to book",
                extra.cl_ord_id, extra.book_id, extra.cum_qty_e2
            );
        }

        report
    });

    assert_eq!(report.status, OrdStatus::Filled as i32);
    assert_eq!(report.book_id, 1);
    assert_eq!(report.instrument.as_ref().unwrap().instrument_id, 1001);
    // The target is absolute: D1 must trade only the shortfall between it and
    // the position the startup order already established, not the full target.
    assert_eq!(report.cum_qty_e2, TARGET_QTY_E2 - STARTUP_QTY_E2);
    assert_eq!(report.leaves_qty_e2, 0);

    shutdown.store(true, Ordering::Relaxed);
    let _ = handles.core.join();
    let _ = handles.feed.join();
    let _ = handles.fix.join();
    let _ = handles.nats.join();
}
