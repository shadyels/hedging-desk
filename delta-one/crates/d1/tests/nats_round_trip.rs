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
// `d1::run_core` places the CLI startup order as ClOrdId seq 1, then hands
// out seq 2 to the first NATS-driven target -- see lib.rs's `next_seq`.
const TARGET_DRIVEN_CL_ORD_ID: &str = "00000000000000000002";

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

    let shutdown = Arc::new(AtomicBool::new(false));
    let handles = spawn(
        StartupOrder {
            book: BookId(1),
            instrument: InstrumentId(1001),
            side: Side::Buy,
            qty_e2: 100, // small startup order, distinct from the target-driven one below
            px_e9: 0,
        },
        FixConfig {
            settings_path: initiator_cfg_path,
            sender_comp_id: sender_comp_id(),
            target_comp_id: target_comp_id(),
        },
        NATS_URL.to_string(),
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
            target_qty_e2: 5_000, // 50.00 units, buy (positive)
            ..Default::default()
        };
        let payload = target.encode_to_vec();

        // Core NATS pub/sub doesn't queue for a subscriber that isn't
        // registered yet -- `d1::spawn`'s NATS gateway thread needs a moment
        // to connect + subscribe after `spawn` returns, so a single publish
        // can race it and be silently dropped. Republish whenever a full
        // window passes with nothing at all on the exec subject (that
        // signals the first publish never landed, not just that the
        // matching report hasn't shown up yet -- the startup order's own
        // report arrives first regardless, proving the subscription is
        // alive, so once anything arrives we stop republishing).
        const RETRY_WINDOW: Duration = Duration::from_secs(2);
        let deadline = Instant::now() + ROUND_TRIP_TIMEOUT;
        client
            .publish("exo.targets.1.1001", payload.clone().into())
            .await
            .expect("publish TargetPosition");

        loop {
            assert!(
                Instant::now() < deadline,
                "ExecutionReport for the target-driven order never arrived"
            );
            match tokio::time::timeout(RETRY_WINDOW, subscriber.next()).await {
                Ok(Some(msg)) => {
                    let report =
                        ExecutionReport::decode(msg.payload).expect("decode ExecutionReport");
                    if report.cl_ord_id == TARGET_DRIVEN_CL_ORD_ID {
                        break report;
                    }
                    // Not our target-driven order's report (e.g. the CLI
                    // startup order's) -- subscription is alive, keep
                    // waiting without republishing.
                }
                Ok(None) => panic!("subscription ended early"),
                Err(_) => {
                    client
                        .publish("exo.targets.1.1001", payload.clone().into())
                        .await
                        .expect("republish TargetPosition");
                }
            }
        }
    });

    assert_eq!(report.status, OrdStatus::Filled as i32);
    assert_eq!(report.book_id, 1);
    assert_eq!(report.instrument.as_ref().unwrap().instrument_id, 1001);
    assert_eq!(report.cum_qty_e2, 5_000);
    assert_eq!(report.leaves_qty_e2, 0);

    shutdown.store(true, Ordering::Relaxed);
    let _ = handles.core.join();
    let _ = handles.feed.join();
    let _ = handles.fix.join();
    let _ = handles.nats.join();
}
