//! P1.M4 Slice 3 DoD proof: drive the exact same crosses/transfers storyline
//! as `crosses_round_trip.rs` (startup fill -> book-1 band-suppressed target
//! -> book-2 opposite target -> internal cross + residual external fill ->
//! directed `InternalTransferRequest`), but with a real Kafka broker wired in
//! (`d1::spawn`'s `posttrade_cfg: Some(..)`, `deterministic: true`), then
//! diffs the resulting `posttrade.*` Avro records -- decoded to one compact
//! JSON object per line -- against the committed golden files in
//! `sim/golden/`. `d1-posttrade::Stamper::Fixed` pins every id/timestamp and
//! deterministic feed drift (`0`) pins every price, so two runs against a
//! clean broker are expected to be byte-for-byte identical Avro (and
//! therefore line-for-line identical JSON).
//!
//! Requires a NATS server on `127.0.0.1:4222`, a Kafka broker on
//! `127.0.0.1:9092` with the five `posttrade.*` topics already provisioned
//! (`just up`, then `scripts/demo.sh`'s topic-create step -- topics are
//! **not** created here, matching `d1-posttrade::run_producer`'s own
//! hard-error-on-missing-topic contract), and a Confluent Schema Registry on
//! `127.0.0.1:8081`. Schema *subjects*, unlike topics, are **not** provisioned
//! out of band: `run_producer` registers all five itself at startup
//! (idempotently), so this test passes against a registry with no subjects.
//!
//! Every consumed record is checked for the Confluent frame
//! (`d1_posttrade::registry::frame`) before decoding, and the golden fixtures
//! store *decoded* JSON -- so they are unchanged by framing and act as an
//! independent check that the 5-byte prefix did not disturb the datum.
//! Marked `#[ignore]` so plain `cargo
//! test`/`just test` stays green without Docker; run explicitly with
//! `--ignored`. Also auto-skips at runtime if either server turns out to be
//! unreachable. Mirrors `crosses_round_trip.rs`'s setup/skip pattern exactly,
//! extended with a Kafka watermark-bracketed consume + golden-file diff.
//!
//! `UPDATE_GOLDEN=1` rewrites `sim/golden/posttrade.*.jsonl` instead of
//! asserting against them.
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::indexing_slicing
)] // integration test, not hot-path code

use std::collections::HashMap;
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::{Duration, Instant};

use apache_avro::Schema;
use d1::config::TrackerConfig;
use d1::{FixConfig, PostTradeConfig, StartupOrder, spawn};
use d1_core::{BookId, InstrumentId, Side};
use d1_gateway_nats::pb::hedging::common::v1::{InstrumentRef, Meta};
use d1_gateway_nats::pb::hedging::live::v1::{
    ExecutionReport, InternalCrossNotice, InternalTransferRequest, OrdStatus, TargetPosition,
};
use d1_posttrade::{
    FIXED_BOOKED_NS, FRAME_LEN, MAGIC_BYTE, TOPIC_ALLOCATIONS, TOPIC_CROSSES, TOPIC_ORDER_AUDIT,
    TOPIC_TRACKER_ANALYTICS, TOPIC_TRADES,
};
use futures_util::StreamExt;
use prost::Message as _;
use rdkafka::Offset;
use rdkafka::config::ClientConfig;
use rdkafka::consumer::{BaseConsumer, Consumer};
use rdkafka::message::Message as _;
use rdkafka::topic_partition_list::TopicPartitionList;

const NATS_URL: &str = "127.0.0.1:4222";
const KAFKA_BROKERS: &str = "127.0.0.1:9092";
/// Confluent Schema Registry (`deploy/docker-compose.yml`). The producer
/// registers the five post-trade schemas here at startup and frames every
/// record with the id it gets back (ADR-002).
const SCHEMA_REGISTRY: &str = "http://127.0.0.1:8081";
const SCHEMA_REGISTRY_ADDR: &str = "127.0.0.1:8081";
// Distinct from every other tests/*.rs FIX_PORT -- quickfix's SessionID
// registry is process-global, but each `tests/*.rs` integration test file
// compiles to its own binary/process, so this only needs to avoid the OS
// still holding another test's port from a very recent run.
const FIX_PORT: u16 = 15_032;
const ROUND_TRIP_TIMEOUT: Duration = Duration::from_secs(15);
const KAFKA_METADATA_TIMEOUT: Duration = Duration::from_secs(10);
const KAFKA_CONSUME_TIMEOUT: Duration = Duration::from_secs(20);
const KAFKA_POLL_INTERVAL: Duration = Duration::from_millis(500);

/// Startup order books directly to book 1 / instrument 1001 -- unrelated to
/// the cross demo below (instrument 1002), kept only as the FIX round-trip
/// anchor `d1::spawn` requires and as the sync point proving the gateway's
/// subscriptions are live (same reasoning as `crosses_round_trip.rs`).
const STARTUP_CL_ORD_ID: &str = "00000000000000000001";
const STARTUP_QTY_E2: i64 = 100;

/// Cross-demo instrument: MSFT, distinct from the startup pair (AAPL,
/// instrument 1001) so the startup fill never perturbs this cycle's demand.
const CROSS_INSTRUMENT_ID: u32 = 1002;
const BOOK1_TARGET_QTY_E2: i64 = 1_000_000;
const BOOK1_BAND_E2: i64 = 1_000_000;
const BOOK2_TARGET_QTY_E2: i64 = -800_000;
const EXPECTED_CROSS_QTY_E2: i64 = 800_000;
const EXPECTED_RESIDUAL_QTY_E2: i64 = 200_000;
const RESIDUAL_CL_ORD_ID: &str = "00000000000000000002";

/// Directed-transfer demo: RATES-IR (book 5, ADR-008/009) receives risk from
/// book 1 on the same cross-demo instrument.
const TRANSFER_FROM_BOOK: u32 = 1;
const TRANSFER_TO_BOOK: u32 = 5;
const TRANSFER_QTY_E2: i64 = 10_000;

/// The five `posttrade.*` topics, in the order golden files are produced.
/// `TOPIC_TRACKER_ANALYTICS` is consumed and count-checked like the other
/// four, but -- unlike them -- is never byte-diffed against a committed
/// fixture (see `assert_tracker_record`'s doc comment).
const TOPICS: [&str; 5] = [
    TOPIC_TRADES,
    TOPIC_CROSSES,
    TOPIC_ALLOCATIONS,
    TOPIC_ORDER_AUDIT,
    TOPIC_TRACKER_ANALYTICS,
];

/// Expected record count per topic for this exact storyline (startup fill,
/// book1/book2 cross + residual, directed transfer, one end-of-session
/// tracker record). Asserted unconditionally -- including under
/// `UPDATE_GOLDEN=1` -- so a half-broken run (e.g. a dropped ring push) can
/// never regenerate a golden file that then passes vacuously on every later
/// run (LOW-3).
const EXPECTED_COUNTS: [(&str, usize); 5] = [
    (TOPIC_TRADES, 6),
    (TOPIC_CROSSES, 2),
    (TOPIC_ALLOCATIONS, 1),
    (TOPIC_ORDER_AUDIT, 4),
    (TOPIC_TRACKER_ANALYTICS, 1),
];

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

/// `sim/golden/` lives at the **git repo root** (`sim/CLAUDE.md`: "golden
/// outputs (`golden/`, created in P1.M4)"), not under `delta-one/` -- same
/// three-parents-up depth as `universe_path()` above, into a different
/// top-level sibling directory. Deliberately NOT `workspace_root()`, which
/// resolves to `delta-one/` (the Cargo workspace root the other helpers in
/// this file need for `cargo run -p sim`), one level short of the repo root.
fn golden_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../sim/golden")
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

fn kafka_reachable() -> bool {
    TcpStream::connect_timeout(
        &KAFKA_BROKERS
            .parse()
            .expect("KAFKA_BROKERS is a valid socket addr"),
        Duration::from_millis(300),
    )
    .is_ok()
}

fn schema_registry_reachable() -> bool {
    TcpStream::connect_timeout(
        &SCHEMA_REGISTRY_ADDR
            .parse()
            .expect("SCHEMA_REGISTRY_ADDR is a valid socket addr"),
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

/// Parsed Avro schemas for the four `protocol/avro/` post-trade records,
/// `include_str!`'d the same way `d1-posttrade/src/convert.rs` does (this
/// test file lives at the same directory depth from the repo root:
/// `delta-one/crates/d1/tests/` vs. `delta-one/crates/d1-posttrade/src/`).
struct GoldenSchemas {
    trades: Schema,
    crosses: Schema,
    allocations: Schema,
    order_audit: Schema,
    tracker: Schema,
}

impl GoldenSchemas {
    fn load() -> Self {
        Self {
            trades: Schema::parse_str(include_str!(
                "../../../../protocol/avro/posttrade_trade.avsc"
            ))
            .expect("parse posttrade_trade.avsc"),
            crosses: Schema::parse_str(include_str!(
                "../../../../protocol/avro/posttrade_cross.avsc"
            ))
            .expect("parse posttrade_cross.avsc"),
            allocations: Schema::parse_str(include_str!(
                "../../../../protocol/avro/posttrade_allocation.avsc"
            ))
            .expect("parse posttrade_allocation.avsc"),
            order_audit: Schema::parse_str(include_str!(
                "../../../../protocol/avro/order_audit.avsc"
            ))
            .expect("parse order_audit.avsc"),
            tracker: Schema::parse_str(include_str!(
                "../../../../protocol/avro/tracker_analytics.avsc"
            ))
            .expect("parse tracker_analytics.avsc"),
        }
    }

    fn for_topic(&self, topic: &str) -> &Schema {
        match topic {
            TOPIC_TRADES => &self.trades,
            TOPIC_CROSSES => &self.crosses,
            TOPIC_ALLOCATIONS => &self.allocations,
            TOPIC_ORDER_AUDIT => &self.order_audit,
            TOPIC_TRACKER_ANALYTICS => &self.tracker,
            other => panic!("golden_posttrade: no schema mapped for topic {other}"),
        }
    }
}

fn build_kafka_consumer() -> BaseConsumer {
    ClientConfig::new()
        .set("bootstrap.servers", KAFKA_BROKERS)
        // `assign()` (manual partition assignment, no `subscribe()`/group
        // rebalance) still requires `group.id` to be set -- librdkafka
        // rejects `rd_kafka_assign` with "Unknown group" otherwise. This
        // consumer never joins a group (no `subscribe`), so the id itself
        // is inert; any value works.
        .set("group.id", "d1-golden-posttrade-test")
        .set("enable.auto.commit", "false")
        .create()
        .expect("create Kafka consumer")
}

/// Current high watermark (next offset to be written) for `topic`'s single
/// partition (`scripts/demo.sh` creates every `posttrade.*` topic with
/// `--partitions 1`, which is also what makes per-topic record order
/// deterministic).
fn high_watermark(consumer: &BaseConsumer, topic: &str) -> i64 {
    // `consume_range`/this function both hardcode partition 0. A topic
    // created with more than one partition would silently scatter records
    // across partitions, making per-topic record order (and this golden
    // diff) undefined -- fail loudly here instead of leaving a confusing
    // diff for whoever hits it (LOW-8).
    let metadata = consumer
        .fetch_metadata(Some(topic), KAFKA_METADATA_TIMEOUT)
        .unwrap_or_else(|err| {
            panic!(
                "golden_posttrade: fetch_metadata({topic}) failed: {err} -- provision the four posttrade.* topics first (see scripts/demo.sh)"
            )
        });
    let partition_count = metadata
        .topics()
        .first()
        .map(|t| t.partitions().len())
        .unwrap_or(0);
    assert_eq!(
        partition_count, 1,
        "golden_posttrade: topic {topic} has {partition_count} partition(s), expected exactly 1 -- this test only reads partition 0, so record order (and the golden diff) is undefined otherwise (scripts/demo.sh creates every posttrade.* topic with --partitions 1)"
    );

    let (_low, high) = consumer
        .fetch_watermarks(topic, 0, KAFKA_METADATA_TIMEOUT)
        .unwrap_or_else(|err| {
            panic!(
                "golden_posttrade: fetch_watermarks({topic}) failed: {err} -- provision the four posttrade.* topics first (see scripts/demo.sh)"
            )
        });
    high
}

/// Strip and validate the Confluent wire-format prefix
/// (`0x00 || schema_id BE32`, `d1_posttrade::registry::frame`), returning the
/// schema id and the bare Avro datum.
///
/// Asserting here rather than blindly slicing is deliberate: an unframed or
/// mis-framed record would otherwise still decode (Avro datums are not
/// self-delimiting, so a 5-byte shift usually yields *some* value) and the
/// golden diff would report a confusing field-level mismatch instead of the
/// actual fault.
fn split_confluent_frame(topic: &str, payload: &[u8]) -> (u32, Vec<u8>) {
    assert!(
        payload.len() >= FRAME_LEN,
        "golden_posttrade: {topic} record is {} byte(s), shorter than the {FRAME_LEN}-byte Confluent frame -- is the producer still publishing raw Avro datums?",
        payload.len()
    );
    let (prefix, datum) = payload.split_at(FRAME_LEN);
    assert_eq!(
        prefix[0], MAGIC_BYTE,
        "golden_posttrade: {topic} record has magic byte {:#04x}, expected {MAGIC_BYTE:#04x} (ADR-002 Confluent wire format)",
        prefix[0]
    );
    let schema_id = u32::from_be_bytes([prefix[1], prefix[2], prefix[3], prefix[4]]);
    assert!(
        schema_id > 0,
        "golden_posttrade: {topic} record carries schema id 0 -- the registry never assigns 0, so the prefix is not a real frame"
    );
    (schema_id, datum.to_vec())
}

/// Decode one framed post-trade record against `schema` and re-encode it as a
/// single compact JSON line, via `apache_avro`'s `TryFrom<types::Value> for
/// serde_json::Value` (`apache-avro` 0.21 `src/types.rs`).
///
/// The JSON this produces is what `sim/golden/*.jsonl` stores, so those
/// fixtures are unchanged by framing — which makes them an independent check
/// that the prefix was added without disturbing the datum.
fn decode_to_json_line(schema: &Schema, topic: &str, payload: &[u8]) -> String {
    let (_schema_id, datum) = split_confluent_frame(topic, payload);
    let avro_value = apache_avro::from_avro_datum(schema, &mut &datum[..], None)
        .expect("decode Avro datum against schema");
    let json = serde_json::Value::try_from(avro_value).expect("Avro value to JSON");
    serde_json::to_string(&json).expect("serialize JSON line")
}

/// Consume exactly `end - start` records from `topic`'s partition 0, starting
/// at offset `start` (the high watermark recorded before this test drove any
/// traffic), decoding each to a JSON line via `schema`. Assigns/unassigns the
/// shared `consumer` around the read since the four topics are consumed
/// sequentially, not concurrently.
fn consume_range(
    consumer: &BaseConsumer,
    topic: &str,
    start: i64,
    end: i64,
    schema: &Schema,
) -> Vec<String> {
    let want = end.saturating_sub(start);
    if want <= 0 {
        return Vec::new();
    }
    let want = usize::try_from(want).unwrap_or(0);

    let mut tpl = TopicPartitionList::new();
    tpl.add_partition_offset(topic, 0, Offset::Offset(start))
        .expect("set partition offset");
    consumer.assign(&tpl).expect("assign partition");

    let mut lines = Vec::with_capacity(want);
    let deadline = Instant::now() + KAFKA_CONSUME_TIMEOUT;
    while lines.len() < want {
        assert!(
            Instant::now() < deadline,
            "golden_posttrade: timed out consuming {topic}, got {} of {want} expected record(s)",
            lines.len()
        );
        match consumer.poll(KAFKA_POLL_INTERVAL) {
            Some(Ok(msg)) => {
                let payload = msg.payload().expect("message has a payload");
                lines.push(decode_to_json_line(schema, topic, payload));
            }
            Some(Err(err)) => panic!("golden_posttrade: consume {topic} failed: {err}"),
            None => {}
        }
    }
    consumer.unassign().expect("unassign partition");
    lines
}

/// Compare `lines` against `sim/golden/<topic>.jsonl`, or (with
/// `UPDATE_GOLDEN=1` set) overwrite the golden file with `lines` instead of
/// asserting. On mismatch, panics with a readable per-line diff -- a golden
/// test whose failure output is an unreadable blob is useless at 3am.
fn compare_or_update_golden(topic: &str, golden_path: &Path, lines: &[String]) {
    // `.is_ok()` would also treat `UPDATE_GOLDEN=0` as "update" -- match the
    // value instead (LOW-4).
    if std::env::var("UPDATE_GOLDEN").as_deref() == Ok("1") {
        // A half-broken run (e.g. a dropped ring push, an early panic caught
        // by a retry) could otherwise regenerate an empty golden file that
        // then passes vacuously on every later run (LOW-3). The per-topic
        // count assert in `posttrade_golden_file` already catches this
        // before we get here, but this is the load-bearing last line of
        // defense for the actual write.
        assert!(
            !lines.is_empty(),
            "golden_posttrade: refusing to write an empty golden file for {topic} -- the run produced 0 records, which is never correct for this storyline"
        );
        let mut content = lines.join("\n");
        content.push('\n');
        std::fs::write(golden_path, content).expect("write golden file");
        println!(
            "golden_posttrade: UPDATE_GOLDEN wrote {} ({} line(s))",
            golden_path.display(),
            lines.len()
        );
        return;
    }

    let expected_content = std::fs::read_to_string(golden_path).unwrap_or_else(|err| {
        panic!(
            "golden_posttrade: missing golden file {} for topic {topic} ({err}) -- run with UPDATE_GOLDEN=1 to create it",
            golden_path.display()
        )
    });
    let expected_lines: Vec<&str> = expected_content.lines().collect();

    let mut mismatches = Vec::new();
    let max_len = expected_lines.len().max(lines.len());
    for i in 0..max_len {
        let expected = expected_lines.get(i).copied();
        let actual = lines.get(i).map(String::as_str);
        if expected != actual {
            mismatches.push(format!(
                "  line {}:\n    expected: {}\n    actual:   {}",
                i + 1,
                expected.unwrap_or("<missing>"),
                actual.unwrap_or("<missing>")
            ));
        }
    }

    assert!(
        mismatches.is_empty(),
        "golden_posttrade: {topic} mismatch ({} expected line(s), {} actual line(s)):\n{}",
        expected_lines.len(),
        lines.len(),
        mismatches.join("\n")
    );
    println!(
        "golden_posttrade: {topic} matches golden ({} line(s))",
        lines.len()
    );
}

/// Property-assert (never byte-diff) the single `posttrade.tracker.analytics`
/// record this storyline produces. `exch_ts_ns` is deterministic per tick,
/// but the NUMBER of ticks before shutdown depends on real handshake timing,
/// so `n_obs` -- the variance denominator -- varies run to run; a committed
/// byte-for-byte fixture here would be permanently flaky. `lines` has
/// already been through `decode_to_json_line` -> `split_confluent_frame`, so
/// the Confluent-frame check has already happened by the time this runs.
fn assert_tracker_record(lines: &[String]) {
    let parsed: serde_json::Value = lines
        .first()
        .map(|line| serde_json::from_str(line).expect("parse tracker JSON line"))
        .unwrap_or_else(|| panic!("golden_posttrade: no tracker record to assert on"));

    assert_eq!(parsed["book_id"].as_i64(), Some(1), "book_id");
    assert_eq!(
        parsed["benchmark_symbol"].as_str(),
        Some("SPX"),
        "benchmark_symbol"
    );
    assert_eq!(parsed["kind"].as_str(), Some("EX_POST"), "kind");

    let n_obs = parsed["n_obs"].as_i64().expect("n_obs is a JSON number");
    assert!(
        n_obs >= 2,
        "n_obs must be >= 2 (analytics()'s own floor), got {n_obs}"
    );

    let window_start_ns = parsed["window_start_ns"]
        .as_i64()
        .expect("window_start_ns is a JSON number");
    let window_end_ns = parsed["window_end_ns"]
        .as_i64()
        .expect("window_end_ns is a JSON number");
    assert!(
        window_start_ns < window_end_ns,
        "window_start_ns ({window_start_ns}) must be < window_end_ns ({window_end_ns})"
    );
    // The permanent guard against regressing to `Stamper::now_ns` for this
    // field: under `Stamper::Fixed` that is the constant `FIXED_BOOKED_NS`,
    // which would make `window_start_ns == window_end_ns` a plausible-looking
    // (but wrong) fixed value instead of the feed's real synthetic clock.
    assert_ne!(
        window_start_ns, FIXED_BOOKED_NS,
        "window_start_ns must come from the feed's exch_ts_ns clock, never Stamper::now_ns"
    );
}

#[test]
#[ignore = "requires a NATS server on 127.0.0.1:4222, a Kafka broker on 127.0.0.1:9092 with posttrade.* topics provisioned, and a Schema Registry on 127.0.0.1:8081 (`just up`, then scripts/demo.sh's topic-create step)"]
fn posttrade_golden_file() {
    if !nats_reachable() {
        println!("golden_posttrade: NATS unreachable at {NATS_URL}, skipping (`just up` first)");
        return;
    }
    if !kafka_reachable() {
        println!(
            "golden_posttrade: Kafka unreachable at {KAFKA_BROKERS}, skipping (`just up` first)"
        );
        return;
    }
    if !schema_registry_reachable() {
        println!(
            "golden_posttrade: Schema Registry unreachable at {SCHEMA_REGISTRY}, skipping (`just up` first)"
        );
        return;
    }

    let consumer = build_kafka_consumer();
    // Before driving anything: record each topic's high watermark so this
    // test is correct on a dirty broker (scripts/demo.sh runs
    // nats_round_trip.rs and crosses_round_trip.rs first, which also
    // produce to these topics) -- consume only the slice this run itself
    // appends, never anything already sitting on the topic.
    let hwm_start: HashMap<&str, i64> = TOPICS
        .iter()
        .map(|&topic| (topic, high_watermark(&consumer, topic)))
        .collect();

    let tmp = std::env::temp_dir().join(format!("d1-golden-posttrade-{FIX_PORT}"));
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
    let book_ids = universe.book_ids.clone();
    let instrument_ids = universe.instrument_ids.clone();
    // Tracker analytics needs real feed run time, not just round-trip
    // handshake time -- see the wait after the storyline below for why this
    // is captured here, right before the feed thread starts.
    let run_start = Instant::now();
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
        // `sampling_interval_s: 1` (carried over from Slice 2). Getting
        // `assert_tracker_record`'s `n_obs >= 2` floor needs the THIRD
        // sampling-boundary crossing of the feed's `exch_ts_ns` clock (see
        // `TrackerState::sample`'s doc comment in `crates/d1/src/lib.rs`),
        // which -- given the feed's real-time 500ms tick cadence
        // (`crates/d1/src/feed.rs::TICK_INTERVAL`) -- takes several real
        // seconds of feed run time, well past what the FIX/NATS handshake
        // alone burns; `run_start`/`TRACKER_MIN_RUNTIME` below top that up.
        TrackerConfig {
            sampling_interval_s: 1,
            te_window_obs: 250,
            publish_interval_s: 1,
        },
        Some(PostTradeConfig {
            brokers: KAFKA_BROKERS.to_string(),
            registry_url: SCHEMA_REGISTRY.to_string(),
        }),
        true, // deterministic: pinned ids/timestamps/mid, the golden-file path
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
        // the startup fill is booked, exactly as `crosses_round_trip.rs`
        // reasons about it.
        let startup = await_report(&mut startup_subscriber, STARTUP_CL_ORD_ID).await;
        assert_eq!(startup.status, OrdStatus::Filled as i32);
        assert_eq!(startup.book_id, 1);

        // Book 1's own target: alone, its band fully suppresses the
        // external order, so no ExecutionReport/InternalCrossNotice comes
        // out of this publish -- its demand simply stays live for the next
        // cycle.
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
        // cycle, crossing 800,000 and leaving a 200,000 residual external
        // buy.
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

    // All NATS sync points above only prove the corresponding `cross_tx`/
    // `exec_report_tx` pushes happened. That is enough to conclude every
    // post-trade event is already enqueued ONLY because `run_core` pushes to
    // `posttrade_tx` before the matching NATS ring on every path -- the fill
    // path (`push_posttrade` calls precede `exec_report_tx.push`) and both
    // cross paths (netting-derived and directed transfer, where
    // `posttrade::cross_events` precedes `cross_tx.push`). That ordering is
    // load-bearing for this test: reverse either cross path and the last
    // sync point can return before its `TradeLeg`/`Cross` records are
    // enqueued, truncating the golden file.
    //
    // The 250ms sleep below is genuine belt-and-braces given that ordering,
    // not a substitute for it -- it only gives the core thread's poll loop
    // one more spin before shutdown is signalled.
    //
    // `TRACKER_MIN_RUNTIME` is a SEPARATE wait, for a different reason: the
    // tracker record needs the THIRD crossing of the feed's `exch_ts_ns`
    // sampling boundary (`TrackerState::sample`'s doc comment,
    // `crates/d1/src/lib.rs`) to reach `n_obs >= 2`, and the feed only
    // advances that clock in real time, `feed::TICK_INTERVAL` (500ms) per
    // wave (`crates/d1/src/feed.rs::run_feed_producer`). With
    // `sampling_interval_s: 1` that crossing lands ~3 real seconds after the
    // feed thread starts (waves at 0/.5/1.0/1.5/2.0/2.5/3.0s; the wave
    // ticking `exch_ts_ns == 3_000_000_000` is the third crossing) --
    // comfortably past the FIX/NATS round trip above, which the sim-side log
    // shows completing in roughly a second. Topping up to a fixed floor
    // (rather than sleeping the whole floor unconditionally on every run)
    // keeps a fast run from waiting longer than it has to.
    //
    // ponytail: coupled to `feed::TICK_INTERVAL`'s real-time cadence, the one
    // ceiling every synthetic-feed test already accepts (`feed.rs`'s own doc
    // comment) -- a real feed replaces this with an actual EOD boundary, not
    // a wall-clock budget in a test.
    const TRACKER_MIN_RUNTIME: Duration = Duration::from_millis(4_000);
    if let Some(remaining) = TRACKER_MIN_RUNTIME.checked_sub(run_start.elapsed()) {
        thread::sleep(remaining);
    }
    thread::sleep(Duration::from_millis(250));

    shutdown.store(true, Ordering::Relaxed);
    let _ = handles.core.join();
    let _ = handles.feed.join();
    let _ = handles.fix.join();
    let _ = handles.nats.join();
    // Ordered-shutdown contract (`d1::RunHandles::posttrade_shutdown` doc
    // comment): the producer thread has its OWN shutdown flag, only
    // signalled here -- after `core` (the only pusher onto `posttrade_tx`)
    // has already been joined above -- so the producer's final drain is
    // guaranteed to see every event `core` ever produced instead of racing
    // it.
    match (handles.posttrade, handles.posttrade_shutdown) {
        (Some(posttrade), Some(flag)) => {
            flag.store(true, Ordering::Relaxed);
            match posttrade.join() {
                Ok(Ok(())) => {}
                Ok(Err(err)) => panic!("posttrade producer thread exited with error: {err}"),
                Err(_) => panic!("posttrade producer thread panicked"),
            }
        }
        _ => panic!("posttrade producer thread was never spawned (posttrade_cfg was None)"),
    }

    let hwm_end: HashMap<&str, i64> = TOPICS
        .iter()
        .map(|&topic| (topic, high_watermark(&consumer, topic)))
        .collect();

    let schemas = GoldenSchemas::load();
    let golden_dir = golden_dir();
    std::fs::create_dir_all(&golden_dir).expect("create sim/golden dir");

    for &topic in &TOPICS {
        let start = *hwm_start.get(topic).expect("hwm_start has every topic");
        let end = *hwm_end.get(topic).expect("hwm_end has every topic");
        let lines = consume_range(&consumer, topic, start, end, schemas.for_topic(topic));
        println!(
            "golden_posttrade: {topic} {} record(s) (offsets {start}..{end})",
            lines.len()
        );
        // Asserted outside (before) the golden comparison, unconditionally,
        // so this invariant survives an `UPDATE_GOLDEN=1` regeneration
        // rather than being silently baked into whatever the run happened
        // to produce (LOW-3).
        let expected = EXPECTED_COUNTS
            .iter()
            .find(|&&(t, _)| t == topic)
            .map(|&(_, count)| count)
            .unwrap_or_else(|| {
                panic!("golden_posttrade: no expected count configured for topic {topic}")
            });
        assert_eq!(
            lines.len(),
            expected,
            "golden_posttrade: {topic} produced {} record(s), expected exactly {expected} for this storyline",
            lines.len()
        );
        // The tracker topic is property-asserted, never byte-diffed
        // (`assert_tracker_record`'s doc comment: `n_obs` depends on real
        // handshake timing, so a committed fixture would be permanently
        // flaky) -- skip `compare_or_update_golden` for it, including under
        // `UPDATE_GOLDEN=1` (there is no `sim/golden/posttrade.tracker.analytics.jsonl`
        // to write).
        if topic == TOPIC_TRACKER_ANALYTICS {
            assert_tracker_record(&lines);
            continue;
        }

        let golden_path = golden_dir.join(format!("{topic}.jsonl"));
        compare_or_update_golden(topic, &golden_path, &lines);
    }
}
