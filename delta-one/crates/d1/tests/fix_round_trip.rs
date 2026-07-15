//! Slice 2 DoD proof: a `NewOrderSingle` leaves an in-process FIX initiator
//! (`d1-gateway-fix::run_initiator`), is filled by a real `sim` acceptor
//! (spawned as a subprocess -- `sim` has no library target, and this is the
//! same real-process split the architecture actually uses), comes back as
//! `ExecutionReport`(s) over a real socket, and drives
//! `d1-core::OrderStore::apply_exec` across the `rtrb` ring / thread
//! boundary this slice built. Real sockets -- the honest session-level
//! testing sim/CLAUDE.md calls for.
#![allow(clippy::unwrap_used, clippy::expect_used)] // integration test, not hot-path code

use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::{Duration, Instant};

use d1_core::{BookId, ClOrdId, ExecEvent, InstrumentId, Order, OrderStatus, OrderStore, Side};
use d1_gateway_fix::FixCallbacks;

const ROUND_TRIP_TIMEOUT: Duration = Duration::from_secs(10);

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
    // crates/d1 -> crates -> delta-one
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

// quickfix's SessionID registry is process-global, keyed by (BeginString,
// SenderCompID, TargetCompID, Qualifier) -- NOT by port or by which Rust
// Initiator/Acceptor object created it. `cargo test` runs these tests
// concurrently in one process, so each test needs a distinct comp-ID pair or
// their sessions collide even on different ports (observed: cross-test
// ResendRequest storms and stalled sessions before this fix). One real `d1`
// process and one real `sim --mode acceptor` process never hit this --
// there's only ever one session per process there.
fn sender_comp_id(port: u16) -> String {
    format!("D1-{port}")
}

fn target_comp_id(port: u16) -> String {
    format!("SIM-{port}")
}

fn acceptor_cfg(port: u16) -> String {
    let sender = target_comp_id(port); // acceptor's Sender is the initiator's Target
    let target = sender_comp_id(port);
    format!(
        "[DEFAULT]\nConnectionType=acceptor\nHeartBtInt=30\nUseDataDictionary=N\nResetOnLogon=Y\nStartTime=00:00:00\nEndTime=23:59:59\n\n[SESSION]\nBeginString=FIX.4.4\nSenderCompID={sender}\nTargetCompID={target}\nSocketAcceptPort={port}\n"
    )
}

fn initiator_cfg(port: u16) -> String {
    let sender = sender_comp_id(port);
    let target = target_comp_id(port);
    format!(
        "[DEFAULT]\nConnectionType=initiator\nReconnectInterval=1\nHeartBtInt=30\nUseDataDictionary=N\nResetOnLogon=Y\nStartTime=00:00:00\nEndTime=23:59:59\n\n[SESSION]\nBeginString=FIX.4.4\nSenderCompID={sender}\nTargetCompID={target}\nSocketConnectHost=127.0.0.1\nSocketConnectPort={port}\n"
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
            "sim acceptor never opened port {port} within {timeout:?}"
        );
        thread::sleep(Duration::from_millis(50));
    }
}

fn spawn_sim_acceptor(cfg_path: &Path, fill_model: &str, port: u16) -> SimAcceptor {
    // acceptor's own Sender is the initiator's Target and vice versa --
    // matches acceptor_cfg()'s [SESSION] block for this port.
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
            fill_model,
            "--cfg",
            cfg_path.to_str().expect("cfg path is valid utf8"),
            "--sender-comp-id",
            &target_comp_id(port),
            "--target-comp-id",
            &sender_comp_id(port),
        ])
        .current_dir(workspace_root())
        .stdout(Stdio::null())
        .stderr(Stdio::inherit())
        .spawn()
        .expect("spawn sim acceptor");
    SimAcceptor { child }
}

/// Place one order against a freshly spawned sim acceptor on `port` running
/// `fill_model`, drive execs until the order reaches a terminal state (or
/// the round-trip timeout), and return the resulting `OrderStore` plus the
/// `ClOrdId` used, so callers can inspect final state or replay an exec.
fn run_round_trip(port: u16, fill_model: &str, order_qty_e2: i64) -> (OrderStore, ClOrdId) {
    let tmp = std::env::temp_dir().join(format!("d1-fix-round-trip-{port}"));
    let _ = std::fs::remove_dir_all(&tmp); // wipe any store/log files a prior run left behind
    std::fs::create_dir_all(&tmp).expect("create tmp cfg dir");
    let acceptor_cfg_path = write_cfg(&tmp, "acceptor.cfg", &acceptor_cfg(port));
    let initiator_cfg_path = write_cfg(&tmp, "initiator.cfg", &initiator_cfg(port));

    let _sim = spawn_sim_acceptor(&acceptor_cfg_path, fill_model, port);
    wait_for_port(port, ROUND_TRIP_TIMEOUT);

    let (mut outbound_tx, outbound_rx) = rtrb::RingBuffer::<Order>::new(8);
    let (inbound_tx, mut inbound_rx) = rtrb::RingBuffer::<ExecEvent>::new(8);

    let shutdown = Arc::new(AtomicBool::new(false));
    let gateway_shutdown = Arc::clone(&shutdown);
    let callbacks = FixCallbacks::new(inbound_tx);
    let sender = sender_comp_id(port);
    let target = target_comp_id(port);
    let gateway_handle = thread::spawn(move || {
        d1_gateway_fix::run_initiator(
            &initiator_cfg_path,
            &sender,
            &target,
            &callbacks,
            outbound_rx,
            &gateway_shutdown,
        )
    });

    let mut store = OrderStore::new(4);
    let cl_ord_id = ClOrdId::from_seq(1);
    let order = Order {
        cl_ord_id,
        book: BookId(1),
        instrument: InstrumentId(1001),
        side: Side::Buy,
        order_qty_e2,
        limit_px_e9: 0,
        status: OrderStatus::New,
        cum_qty_e2: 0,
        leaves_qty_e2: 0,
        last_px_e9: 0,
    };
    store.place(order);

    let deadline = Instant::now() + ROUND_TRIP_TIMEOUT;
    let mut pending = Some(order);
    while pending.is_some() {
        assert!(
            Instant::now() < deadline,
            "never managed to push the order onto the outbound ring"
        );
        match outbound_tx.push(pending.take().expect("checked is_some")) {
            Ok(()) => {}
            Err(rtrb::PushError::Full(returned)) => {
                pending = Some(returned);
                thread::sleep(Duration::from_millis(20));
            }
        }
    }

    while !store
        .get(cl_ord_id)
        .expect("order tracked")
        .status
        .is_terminal()
    {
        assert!(
            Instant::now() < deadline,
            "order never reached a terminal state within {ROUND_TRIP_TIMEOUT:?}"
        );
        match inbound_rx.pop() {
            Ok(event) => {
                store.apply_exec(&event).expect("apply_exec");
            }
            Err(rtrb::PopError::Empty) => thread::sleep(Duration::from_millis(20)),
        }
    }

    shutdown.store(true, Ordering::Relaxed);
    let _ = gateway_handle.join();

    (store, cl_ord_id)
}

#[test]
fn immediate_full_fill_reaches_filled_and_dedupes_replay() {
    let order_qty_e2 = 10_000; // 100.00 units
    let (mut store, cl_ord_id) = run_round_trip(15_025, "immediate", order_qty_e2);

    let order = store.get(cl_ord_id).expect("order tracked");
    assert_eq!(order.status, OrderStatus::Filled);
    assert_eq!(order.cum_qty_e2, order_qty_e2);
    assert_eq!(order.leaves_qty_e2, 0);

    // sim's acceptor mints exec ids deterministically per fresh process
    // ("EXEC-1" for ImmediateFull's one and only exec). Replaying the exact
    // exec that was already applied must be a deduped no-op (root CLAUDE.md
    // invariant 4), not a second fill on top of an already-terminal order.
    let exec_id = d1_gateway_fix::convert::exec_id_from_fix("EXEC-1").expect("exec id");
    let replay = store
        .apply_exec(&ExecEvent {
            cl_ord_id,
            exec_id,
            reported_status: OrderStatus::Filled,
            last_qty_e2: order_qty_e2,
            last_px_e9: order.last_px_e9,
        })
        .expect("apply_exec");
    assert_eq!(
        replay, None,
        "replayed ExecID must be deduped, not re-applied"
    );
    assert_eq!(
        store.get(cl_ord_id).expect("order tracked").cum_qty_e2,
        order_qty_e2,
        "replay must not double-count the fill"
    );
}

#[test]
fn partial_then_full_fill_reaches_filled() {
    let order_qty_e2 = 10_000; // 100.00 units
    let (store, cl_ord_id) = run_round_trip(15_026, "partial", order_qty_e2);

    let order = store.get(cl_ord_id).expect("order tracked");
    assert_eq!(order.status, OrderStatus::Filled);
    assert_eq!(order.cum_qty_e2, order_qty_e2);
    assert_eq!(order.leaves_qty_e2, 0);
}

#[test]
fn reject_reaches_rejected_with_no_fill() {
    let order_qty_e2 = 10_000;
    let (store, cl_ord_id) = run_round_trip(15_027, "reject", order_qty_e2);

    let order = store.get(cl_ord_id).expect("order tracked");
    assert_eq!(order.status, OrderStatus::Rejected);
    assert_eq!(order.cum_qty_e2, 0);
}
