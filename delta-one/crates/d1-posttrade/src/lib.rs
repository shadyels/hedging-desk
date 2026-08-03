//! d1-posttrade — see delta-one/CLAUDE.md for the crate's role and rules.
//!
//! P1.M4 Slice 1: a pure Avro encoder engine. This crate defines the ring
//! payload types the core thread will eventually publish (Slice 2 wires them
//! onto `rtrb` rings and an `rdkafka` producer thread; none of that exists
//! yet) and `convert.rs`, which maps those payloads to the Avro records in
//! `protocol/avro/`. No Kafka, no threads, no rings here — off the hot path
//! per the crate table, so heap allocation, `String`, wall-clock and Avro
//! are all allowed, but the payload types themselves stay `Copy` and
//! id-based (no instrument symbol/currency string at rest): symbol/currency
//! resolve at the edge from `d1-refdata::Universe`, exactly like
//! `d1-gateway-nats::convert` resolves proto fields.

pub mod convert;
pub mod producer;
pub mod registry;

use std::time::{SystemTime, UNIX_EPOCH};

use d1_analytics::TrackerRecord;
use d1_core::{BookId, ClOrdId, ExecId, InstrumentId, OrderStatus, Side};
use uuid::Uuid;

pub use convert::Schemas;
pub use producer::run_producer;
pub use registry::{FRAME_LEN, MAGIC_BYTE, SchemaIds, frame};

/// Everything the post-trade plane needs to run: a Kafka broker list and the
/// Schema Registry backing it (ADR-002).
///
/// One struct rather than two independent options because the registry is
/// not optional *given* Kafka — a producer that cannot resolve schema ids
/// would publish records no consumer can decode. Bundling them makes
/// "Kafka enabled, registry absent" unconstructable, per `delta-one/CLAUDE.md`'s
/// rule that unrepresentable states should be unconstructable via types.
#[derive(Debug, Clone)]
pub struct PostTradeConfig {
    /// `bootstrap.servers` for the Kafka producer.
    pub brokers: String,
    /// Base URL of the Confluent Schema Registry (e.g. `http://localhost:8081`).
    pub registry_url: String,
}

/// Fixed `booked_ns`/`ts_ns` value `Stamper::Fixed` stamps onto every
/// record, so a golden-file encoder run is reproducible byte-for-byte
/// instead of a wall-clock latency measurement. Obviously synthetic
/// (2023-11-14T22:13:20Z), never a real booking time.
pub const FIXED_BOOKED_NS: i64 = 1_700_000_000_000_000_000;

/// Source of per-record ids and timestamps. `Wall` is the live path --
/// fresh `Uuid::now_v7()` ids, wall-clock timestamps; `Fixed` makes
/// `convert.rs`'s encoder output reproducible for the golden-file e2e test:
/// sequential ids from a counter, one constant timestamp for every record
/// (`FIXED_BOOKED_NS`) -- a fixture is not a latency measurement.
/// `Fixed`'s ids are minted via `Uuid::from_u128`, which does NOT set the
/// version/variant nibbles a real UUIDv7 carries -- a deliberate, contained
/// deviation for readable/sequential golden-file ids, never used on the
/// `Wall` (live) path.
#[derive(Debug)]
pub enum Stamper {
    /// Live path: wall-clock ids and timestamps.
    Wall,
    /// Deterministic path: sequential `Uuid::from_u128` ids starting from
    /// `next`, `FIXED_BOOKED_NS` for every timestamp.
    Fixed {
        /// Next id value `uuid()` will mint; incremented after each call.
        next: u64,
    },
}

impl Stamper {
    /// Mint the next id: `Uuid::now_v7()` under `Wall`, or the next
    /// sequential `Uuid::from_u128(next)` (post-incrementing `next`) under
    /// `Fixed`.
    pub fn uuid(&mut self) -> Uuid {
        match self {
            Self::Wall => Uuid::now_v7(),
            Self::Fixed { next } => {
                let id = Uuid::from_u128(u128::from(*next));
                *next += 1;
                id
            }
        }
    }

    /// Current stamp in nanoseconds since the Unix epoch: the wall clock
    /// under `Wall`, or the constant `FIXED_BOOKED_NS` under `Fixed`.
    pub fn now_ns(&mut self) -> Result<i64, PostTradeError> {
        match self {
            Self::Wall => {
                let elapsed = SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .map_err(PostTradeError::ClockBeforeEpoch)?;
                Ok(i64::try_from(elapsed.as_nanos()).unwrap_or(i64::MAX))
            }
            Self::Fixed { .. } => Ok(FIXED_BOOKED_NS),
        }
    }
}

/// Kafka topic for booked trade legs (ADR-002), keyed by `instrument_id`.
pub const TOPIC_TRADES: &str = "posttrade.trades";
/// Kafka topic for explicit internal crosses (ADR-002/ADR-005), keyed by
/// `cross_id`.
pub const TOPIC_CROSSES: &str = "posttrade.crosses";
/// Kafka topic for pro-rata fill allocations (ADR-002), keyed by
/// `parent_cl_ord_id`.
pub const TOPIC_ALLOCATIONS: &str = "posttrade.allocations";
/// Kafka topic for the order audit trail (ADR-002), keyed by `cl_ord_id`.
pub const TOPIC_ORDER_AUDIT: &str = "posttrade.orders.audit";
/// Kafka topic for the daily index-tracker analytics record (ADR-010 §4),
/// keyed by `book_id`. Unlike the four topics above, exactly one record is
/// published per tracker book per session (`crates/d1/src/lib.rs::run_core`
/// pushes it after the drain loop exits, never on a periodic cadence).
pub const TOPIC_TRACKER_ANALYTICS: &str = "posttrade.tracker.analytics";

/// Destination topic and partition key for one post-trade event. The key is
/// rendered with the exact same id-string logic `convert.rs` uses for the
/// Avro payload's own id fields, so the Kafka key matches the Avro field
/// byte-for-byte wherever that field is itself a string (`cross_id`,
/// `parent_cl_ord_id`, `cl_ord_id`); `Trade`'s `instrument_id` is an Avro
/// `int`, not a string, so its key is that id's plain decimal rendering.
#[must_use]
pub fn topic_and_key(event: &PostTradeEvent) -> (&'static str, String) {
    match event {
        PostTradeEvent::Trade(t) => (TOPIC_TRADES, t.instrument.0.to_string()),
        PostTradeEvent::Cross(c) => (TOPIC_CROSSES, c.cross_id.to_string()),
        PostTradeEvent::Allocation(a) => (
            TOPIC_ALLOCATIONS,
            convert::clordid_to_string(&a.parent_cl_ord_id),
        ),
        PostTradeEvent::OrderAudit(o) => {
            (TOPIC_ORDER_AUDIT, convert::clordid_to_string(&o.cl_ord_id))
        }
        PostTradeEvent::Tracker(r, _sampling_interval_s) => {
            (TOPIC_TRACKER_ANALYTICS, r.book.0.to_string())
        }
    }
}

/// One post-trade event to encode. Mirrors the five Avro record types in
/// `protocol/avro/` (`posttrade_trade`, `posttrade_cross`,
/// `posttrade_allocation`, `order_audit`, `tracker_analytics`).
#[derive(Debug, Clone, Copy)]
pub enum PostTradeEvent {
    /// A booked trade leg (external fill or internal cross leg).
    Trade(TradeLeg),
    /// An explicit internal cross between two books (ADR-005).
    Cross(Cross),
    /// A pro-rata allocation of an external fill back to a book.
    Allocation(Allocation),
    /// An order state transition, for compliance replay.
    OrderAudit(OrderAudit),
    /// One tracker book's end-of-session analytics (ADR-010 §4), plus the
    /// `sampling_interval_s` the record was sampled at
    /// (`d1.toml [tracker]`, `TrackerConfig::sampling_interval_s`).
    /// `TrackerRecord` itself (`d1-analytics`, the pure calculator's output
    /// type) carries no notion of its own sampling cadence -- a consumer
    /// cannot interpret `tracking_error_ann_e9` without knowing how long a
    /// period is, so this is carried alongside the record rather than
    /// threading it as a separate parameter through `Schemas::encode` and
    /// `run_producer`.
    Tracker(TrackerRecord, u32),
}

/// Which side of a trade produced a `TradeLeg`: an external venue fill, or
/// one leg of an internal cross.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TradeKind {
    /// A fill against an external venue.
    ExternalFill,
    /// One leg of an explicit internal cross (ADR-005).
    InternalCrossLeg,
}

/// Booked trade leg (topic `posttrade.trades`, key `instrument_id`). `Copy`
/// and id-based — `convert::encode` resolves `symbol`/`currency` from
/// `d1-refdata::Universe`.
#[derive(Debug, Clone, Copy)]
pub struct TradeLeg {
    /// Book this leg is booked to.
    pub book: BookId,
    /// Instrument traded.
    pub instrument: InstrumentId,
    /// Buy/sell direction.
    pub side: Side,
    /// Quantity, fixed-point x10^2.
    pub qty_e2: i64,
    /// Price, fixed-point x10^9.
    pub px_e9: i64,
    /// Whether this leg is an external fill or an internal cross leg.
    pub kind: TradeKind,
    /// Set for `InternalCrossLeg`: the cross this leg belongs to.
    pub cross_id: Option<Uuid>,
    /// Set for `ExternalFill`: the parent client order id.
    pub parent_cl_ord_id: Option<ClOrdId>,
    /// Set for `ExternalFill`: the exec id of the fill.
    pub exec_id: Option<ExecId>,
}

/// Lineage back to the netting cycle that produced a cross or allocation.
/// The Avro field is a plain string (no union): a directed-transfer cross
/// has no netting cycle, so it encodes to the sentinel `"DIRECT"` instead of
/// widening the wire schema with a nullable/union field for one case.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NettingCycleId {
    /// Generated by netting cycle `n`; encodes as `n.to_string()`.
    Cycle(u64),
    /// A directed transfer, booked outside any netting cycle (ADR-009);
    /// encodes as the sentinel string `"DIRECT"`.
    Direct,
}

/// Explicit internal cross between two books (topic `posttrade.crosses`,
/// key `cross_id`). `Copy` and id-based.
#[derive(Debug, Clone, Copy)]
pub struct Cross {
    /// Compliance-lineage identity for this cross (matches the NATS
    /// `d1.crosses` notice's `cross_id`, `d1_core::CrossRecord`).
    pub cross_id: Uuid,
    /// Instrument crossed.
    pub instrument: InstrumentId,
    /// Book on the buy side.
    pub buy_book: BookId,
    /// Book on the sell side.
    pub sell_book: BookId,
    /// Quantity crossed, fixed-point x10^2.
    pub qty_e2: i64,
    /// Cross reference price, fixed-point x10^9.
    pub ref_px_e9: i64,
    /// Compliance-visible reference-price policy id (ADR-005 s.4).
    pub policy_id: &'static str,
    /// Netting cycle that produced this cross, or `Direct`.
    pub netting_cycle_id: NettingCycleId,
}

/// Pro-rata allocation of an external fill back to a book (topic
/// `posttrade.allocations`, key `parent_cl_ord_id`). `Copy` and id-based.
#[derive(Debug, Clone, Copy)]
pub struct Allocation {
    /// Parent (firm-level) client order id this allocation traces back to.
    pub parent_cl_ord_id: ClOrdId,
    /// Exec id of the fill being allocated.
    pub exec_id: ExecId,
    /// Instrument allocated.
    pub instrument: InstrumentId,
    /// Book this allocation is booked to.
    pub book: BookId,
    /// Quantity allocated, fixed-point x10^2.
    pub qty_e2: i64,
    /// Price, fixed-point x10^9.
    pub px_e9: i64,
    /// Netting cycle that produced this allocation, or `Direct`.
    pub netting_cycle_id: NettingCycleId,
}

/// Origin of an order state transition, for compliance replay.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AuditOrigin {
    /// Emitted by the netting engine's own booking flow.
    NettingEngine,
    /// Triggered by a manual UI action.
    ManualUi,
    /// Any other internal system trigger.
    System,
}

/// An order state transition (topic `posttrade.orders.audit`, key
/// `cl_ord_id`). `Copy` and id-based.
#[derive(Debug, Clone, Copy)]
pub struct OrderAudit {
    /// Client order id transitioning.
    pub cl_ord_id: ClOrdId,
    /// Instrument the order trades.
    pub instrument: InstrumentId,
    /// Buy/sell direction.
    pub side: Side,
    /// Status before the transition.
    pub from_status: OrderStatus,
    /// Status after the transition.
    pub to_status: OrderStatus,
    /// Quantity of this transition's event, fixed-point x10^2.
    pub qty_e2: i64,
    /// Cumulative filled quantity, fixed-point x10^2.
    pub cum_qty_e2: i64,
    /// Remaining unfilled quantity, fixed-point x10^2.
    pub leaves_qty_e2: i64,
    /// What triggered this transition.
    pub origin: AuditOrigin,
    /// Optional free-text detail (e.g. a reject reason).
    pub detail: Option<&'static str>,
}

/// Failure modes for `d1-posttrade`.
#[derive(Debug, thiserror::Error)]
pub enum PostTradeError {
    /// A `BookId`/`InstrumentId` (`u32`) did not fit in the Avro `int`
    /// (`i32`) field it maps to.
    #[error("id {0} does not fit in an Avro `int` (i32)")]
    IdOverflow(u32),
    /// The event named an instrument not present in the loaded
    /// `d1-refdata::Universe` (no symbol/currency to resolve).
    #[error("instrument {0:?} not found in refdata universe")]
    UnknownInstrument(InstrumentId),
    /// The system clock is set before the Unix epoch.
    #[error("system clock is before the Unix epoch")]
    ClockBeforeEpoch(#[source] std::time::SystemTimeError),
    /// Avro schema parse or encode failure.
    #[error(transparent)]
    Avro(#[from] apache_avro::Error),
    /// A Kafka producer or admin (topic-create) operation failed.
    #[error(transparent)]
    Kafka(#[from] rdkafka::error::KafkaError),
    /// One or more required `posttrade.*` topics don't exist on the broker
    /// at startup. `deploy/docker-compose.yml` disables auto-topic-create
    /// (`KAFKA_AUTO_CREATE_TOPICS_ENABLE=false`), so without provisioning
    /// every send to a missing topic would otherwise be silently dropped.
    #[error(
        "missing Kafka topic(s): {0} -- provision them first (see scripts/demo.sh) before starting d1's post-trade producer"
    )]
    MissingTopics(String),
    /// Registering a schema with the Confluent Schema Registry failed —
    /// unreachable registry, or a non-2xx response (e.g. a schema rejected
    /// as BACKWARD-incompatible). Hard failure: without an id there is no
    /// valid Confluent frame, and publishing unframed records would leave
    /// the compliance topics undecodable (ADR-002).
    #[error("schema registry: registering subject {subject} at {url} failed: {source}")]
    SchemaRegistry {
        /// Subject being registered (`<topic>-value`).
        subject: String,
        /// Full URL of the registration request.
        url: String,
        /// Underlying transport or status error.
        source: Box<ureq::Error>,
    },
    /// The registry answered, but not with a usable schema id.
    #[error("schema registry: subject {subject} returned no usable schema id: {body}")]
    SchemaRegistryResponse {
        /// Subject being registered (`<topic>-value`).
        subject: String,
        /// The response body (or a description of why the id was unusable).
        body: String,
    },
    /// A post-trade event mapped to a topic with no registered schema id.
    /// Unreachable for the five `posttrade.*` topics `topic_and_key` emits;
    /// guarded rather than defaulted because a wrong id decodes to garbage.
    #[error("no registered schema id for topic {0}")]
    UnknownTopic(String),
    /// A `u64` timestamp (e.g. `TrackerRecord::window_end_ns`) did not fit in
    /// the Avro `long` (`i64`) field it maps to.
    #[error("timestamp {0} does not fit in an Avro `long` (i64)")]
    TimestampOverflow(u64),
    /// A `TrackerRecord.book` has no resolvable benchmark in the refdata
    /// universe: either the book itself has no entry in
    /// `Universe::tracker_books` (unreachable in practice -- every
    /// `TrackerRecord` this crate ever sees was produced for a book drawn
    /// from that same list, `crates/d1/src/lib.rs::run_core`'s
    /// `tracker_states`), or its `benchmark_symbol` (already validated
    /// against the benchmarks map by `d1-refdata::parse`) is not itself
    /// listed as a quotable instrument in the universe. Guarded rather than
    /// defaulted -- same posture as `UnknownTopic`: a made-up symbol would
    /// silently mislabel a compliance record.
    #[error("book {0:?} has no resolvable tracker benchmark in the refdata universe")]
    UnknownTrackerBenchmark(BookId),
}
