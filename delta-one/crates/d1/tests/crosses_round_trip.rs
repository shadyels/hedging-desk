//! P1.M3 Slice 3 DoD proof: two books post opposite `TargetPosition`s on the
//! same instrument -- book 1's own demand is band-suppressed (no external
//! order yet, so its demand stays fresh, exactly `d1::cycle`'s own
//! `cross_leg_sum_zero_when_two_books_cross`/`adr005_worked_example` setup),
//! then book 2's opposite demand arrives and both are live in the same
//! netting cycle. Asserts the real `d1::spawn` wiring (gateway -> core ->
//! gateway, over a real NATS server + `sim`'s FIX acceptor) publishes one
//! `InternalCrossNotice` on `d1.crosses` for the crossed leg plus a residual
//! `ExecutionReport` on `d1.exec.0.<instrument>` for the uncrossed portion.
//! Then publishes an `InternalTransferRequest` on `exo.transfers.<book>`
//! (ADR-009: RATES-IR, book 5) and asserts the resulting `InternalCrossNotice`
//! on `d1.crosses`, proving the directed-transfer entry point books through
//! the same path.
//!
//! Requires a NATS server on `127.0.0.1:4222` (`just up`). Marked `#[ignore]`
//! so plain `cargo test`/`just test` stays green without Docker; run
//! explicitly with `--ignored`. Also auto-skips at runtime if the server
//! turns out to be unreachable. Mirrors `nats_round_trip.rs`'s setup/skip
//! pattern exactly.
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
use d1_gateway_nats::pb::hedging::live::v1::{
    ExecutionReport, InternalCrossNotice, InternalTransferRequest, OrdStatus, TargetPosition,
};
use futures_util::StreamExt;
use prost::Message;

const NATS_URL: &str = "127.0.0.1:4222";
// Distinct from nats_round_trip.rs's FIX_PORT -- quickfix's SessionID
// registry is process-global, but each `tests/*.rs` integration test file
// compiles to its own binary/process, so this only needs to avoid the OS
// still holding the other tests' ports from a very recent run.
const FIX_PORT: u16 = 15_030;
const ROUND_TRIP_TIMEOUT: Duration = Duration::from_secs(15);

/// Startup order books directly to book 1 / instrument 1001 -- unrelated to
/// the cross demo below (instrument 1002), kept only as the FIX round-trip
/// anchor `d1::spawn` requires and as the sync point proving the gateway's
/// subscriptions are live (same reasoning as `nats_round_trip.rs`).
const STARTUP_CL_ORD_ID: &str = "00000000000000000001";
const STARTUP_QTY_E2: i64 = 100;

/// Cross-demo instrument: MSFT, distinct from the startup pair (AAPL,
/// instrument 1001) so the startup fill never perturbs this cycle's demand.
const CROSS_INSTRUMENT_ID: u32 = 1002;
/// Book 1's own target: large enough that, alone, its band fully suppresses
/// an external order (`band_boundary_equal_is_suppressed`-style: band ==
/// |demand| is suppressed), keeping its demand live for the next cycle.
const BOOK1_TARGET_QTY_E2: i64 = 1_000_000;
const BOOK1_BAND_E2: i64 = 1_000_000;
/// Book 2's opposite target: ADR-005's worked example (`d1-netting`'s
/// `adr005_worked_example` unit test) -- crosses 800,000 against book 1,
/// leaving a 200,000 residual external buy.
const BOOK2_TARGET_QTY_E2: i64 = -800_000;
const EXPECTED_CROSS_QTY_E2: i64 = 800_000;
const EXPECTED_RESIDUAL_QTY_E2: i64 = 200_000;
/// The first (and only, in this test) netting-driven parent order: seq 1 is
/// the startup order, seq 2 is this residual (book 1's own suppressed cycle
/// mints no parent order, so the counter isn't consumed until book 2's
/// target triggers the actual residual).
const RESIDUAL_CL_ORD_ID: &str = "00000000000000000002";

/// Directed-transfer demo: RATES-IR (book 5, ADR-008/009) receives risk from
/// book 1 on the same cross-demo instrument.
const TRANSFER_FROM_BOOK: u32 = 1;
const TRANSFER_TO_BOOK: u32 = 5;
const TRANSFER_QTY_E2: i64 = 10_000;

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

/// Read from `subscriber` until an `InternalCrossNotice` matching
/// `buy_book_id`/`sell_book_id`/`qty_e2` arrives, panicking on
/// `ROUND_TRIP_TIMEOUT`. Notices for other book pairs are skipped, not
/// failed -- mirrors `await_report`'s filter-by-identity shape.
async fn await_cross(
    subscriber: &mut async_nats::Subscriber,
    buy_book_id: u32,
    sell_book_id: u32,
    qty_e2: i64,
) -> InternalCrossNotice {
    let deadline = Instant::now() + ROUND_TRIP_TIMEOUT;
    loop {
        let remaining = deadline
            .checked_duration_since(Instant::now())
            .unwrap_or_default();
        assert!(
            !remaining.is_zero(),
            "no InternalCrossNotice buy_book={buy_book_id} sell_book={sell_book_id} qty_e2={qty_e2} within {ROUND_TRIP_TIMEOUT:?}"
        );
        match tokio::time::timeout(remaining, subscriber.next()).await {
            Ok(Some(msg)) => {
                let notice =
                    InternalCrossNotice::decode(msg.payload).expect("decode InternalCrossNotice");
                if notice.buy_book_id == buy_book_id
                    && notice.sell_book_id == sell_book_id
                    && notice.qty_e2 == qty_e2
                {
                    return notice;
                }
            }
            Ok(None) => panic!("subscription ended early"),
            Err(_) => panic!(
                "no InternalCrossNotice buy_book={buy_book_id} sell_book={sell_book_id} qty_e2={qty_e2} within {ROUND_TRIP_TIMEOUT:?}"
            ),
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

#[test]
#[ignore = "requires a NATS server on 127.0.0.1:4222 (`just up`)"]
fn crosses_and_transfers_round_trip() {
    if !nats_reachable() {
        println!("crosses_round_trip: NATS unreachable at {NATS_URL}, skipping (`just up` first)");
        return;
    }

    let tmp = std::env::temp_dir().join(format!("d1-crosses-round-trip-{FIX_PORT}"));
    let _ = std::fs::remove_dir_all(&tmp);
    std::fs::create_dir_all(&tmp).expect("create tmp cfg dir");
    let acceptor_cfg_path = write_cfg(&tmp, "acceptor.cfg", &acceptor_cfg());
    let initiator_cfg_path = write_cfg(&tmp, "initiator.cfg", &initiator_cfg());

    let _sim = spawn_sim_acceptor(&acceptor_cfg_path);
    wait_for_port(FIX_PORT, ROUND_TRIP_TIMEOUT);

    let universe = d1_refdata::load(&universe_path()).expect("load universe refdata");
    let policy: d1_netting::RefPxPolicy = universe
        .cross_px_policy
        .parse()
        .expect("universe refdata cross_px_policy parses");

    let shutdown = Arc::new(AtomicBool::new(false));
    // Clone the id lists, not `universe` itself: `universe` as a whole moves
    // into `spawn` below for the (unused-in-this-test, no broker running)
    // Kafka producer thread's would-be `symbol`/`currency` resolution.
    let book_ids = universe.book_ids.clone();
    let instrument_ids = universe.instrument_ids.clone();
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
        book_ids,
        instrument_ids,
        policy,
        universe,
        None, // no Kafka broker in this test
        &shutdown,
    );

    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("build tokio runtime");

    runtime.block_on(async {
        let client = async_nats::connect(NATS_URL).await.expect("connect NATS");

        let mut startup_subscriber = client
            .subscribe("d1.exec.1.1001")
            .await
            .expect("subscribe d1.exec.1.1001");
        let mut crosses_subscriber = client
            .subscribe("d1.crosses")
            .await
            .expect("subscribe d1.crosses");
        let mut residual_subscriber = client
            .subscribe(format!("d1.exec.0.{CROSS_INSTRUMENT_ID}"))
            .await
            .expect("subscribe residual exec subject");

        // Sync point: seeing the startup order's own report proves the
        // gateway's subscriptions (targets, transfers) are already live and
        // the startup fill is booked, exactly as `nats_round_trip.rs`
        // reasons about it.
        let startup = await_report(&mut startup_subscriber, STARTUP_CL_ORD_ID).await;
        assert_eq!(startup.status, OrdStatus::Filled as i32);
        assert_eq!(startup.book_id, 1);

        // Book 1's own target: alone, its band fully suppresses the
        // external order, so no ExecutionReport/InternalCrossNotice comes
        // out of this publish -- its demand simply stays live for the next
        // cycle (`d1::cycle`'s `cross_leg_sum_zero_when_two_books_cross`).
        let book1_target = TargetPosition {
            meta: Some(Meta {
                msg_id: "test-cross-book1".to_string(),
                producer: "test".to_string(),
                sent_ns: 1,
                schema_version: 1,
            }),
            book_id: 1,
            instrument: Some(InstrumentRef {
                instrument_id: CROSS_INSTRUMENT_ID,
                ..Default::default()
            }),
            target_qty_e2: BOOK1_TARGET_QTY_E2,
            band_qty_e2: BOOK1_BAND_E2,
            ..Default::default()
        };
        client
            .publish("exo.targets.1.1002", book1_target.encode_to_vec().into())
            .await
            .expect("publish book-1 TargetPosition");

        // Book 2's opposite target: both demands are now live in the same
        // cycle, crossing 800,000 (ADR-005's worked example) and leaving a
        // 200,000 residual external buy.
        let book2_target = TargetPosition {
            meta: Some(Meta {
                msg_id: "test-cross-book2".to_string(),
                producer: "test".to_string(),
                sent_ns: 1,
                schema_version: 1,
            }),
            book_id: 2,
            instrument: Some(InstrumentRef {
                instrument_id: CROSS_INSTRUMENT_ID,
                ..Default::default()
            }),
            target_qty_e2: BOOK2_TARGET_QTY_E2,
            ..Default::default()
        };
        client
            .publish("exo.targets.2.1002", book2_target.encode_to_vec().into())
            .await
            .expect("publish book-2 TargetPosition");

        let cross = await_cross(&mut crosses_subscriber, 1, 2, EXPECTED_CROSS_QTY_E2).await;
        assert_eq!(
            cross.instrument.as_ref().unwrap().instrument_id,
            CROSS_INSTRUMENT_ID
        );
        assert_eq!(cross.px_policy_id, "ARRIVAL_MID");
        assert!(!cross.cross_id.is_empty());

        let residual = await_report(&mut residual_subscriber, RESIDUAL_CL_ORD_ID).await;
        assert_eq!(residual.status, OrdStatus::Filled as i32);
        assert_eq!(residual.book_id, 0);
        assert_eq!(residual.cum_qty_e2, EXPECTED_RESIDUAL_QTY_E2);
        assert_eq!(residual.leaves_qty_e2, 0);

        // Directed transfer (ADR-009): books instantly through the same
        // `book_cross` path, no external order, no parent-order tracking.
        let transfer = InternalTransferRequest {
            meta: Some(Meta {
                msg_id: "test-transfer-1".to_string(),
                producer: "test".to_string(),
                sent_ns: 1,
                schema_version: 1,
            }),
            transfer_id: "test-transfer-1".to_string(),
            instrument: Some(InstrumentRef {
                instrument_id: CROSS_INSTRUMENT_ID,
                ..Default::default()
            }),
            from_book_id: TRANSFER_FROM_BOOK,
            to_book_id: TRANSFER_TO_BOOK,
            qty_e2: TRANSFER_QTY_E2,
            reason: "rho transfer: USD bucket breach".to_string(),
            valuation: None,
        };
        client
            .publish(
                format!("exo.transfers.{TRANSFER_TO_BOOK}"),
                transfer.encode_to_vec().into(),
            )
            .await
            .expect("publish InternalTransferRequest");

        let transfer_cross = await_cross(
            &mut crosses_subscriber,
            TRANSFER_TO_BOOK,
            TRANSFER_FROM_BOOK,
            TRANSFER_QTY_E2,
        )
        .await;
        assert_eq!(
            transfer_cross.instrument.as_ref().unwrap().instrument_id,
            CROSS_INSTRUMENT_ID
        );
        assert_eq!(transfer_cross.px_policy_id, "ARRIVAL_MID");
        assert_ne!(transfer_cross.cross_id, cross.cross_id);
    });

    shutdown.store(true, Ordering::Relaxed);
    let _ = handles.core.join();
    let _ = handles.feed.join();
    let _ = handles.fix.join();
    let _ = handles.nats.join();
}
