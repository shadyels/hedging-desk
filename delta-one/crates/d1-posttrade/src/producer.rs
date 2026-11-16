//! Kafka producer thread (P1.M4 Slice 2): drains the `posttrade` `rtrb` ring,
//! encodes each event via `Schemas::encode`, wraps the datum in the Confluent
//! wire format via `registry::frame`, and publishes it to its `posttrade.*`
//! topic keyed per `topic_and_key` (ADR-002). Non-async, off the hot path --
//! mirrors `d1-gateway-nats::run_gateway`'s poll/drain-loop shape, `rdkafka`'s
//! `ThreadedProducer` instead of `async-nats`.

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::thread;
use std::time::Duration;

use d1_core::RunId;
use d1_refdata::Universe;
use rdkafka::ClientConfig;
use rdkafka::producer::{BaseRecord, DefaultProducerContext, Producer, ThreadedProducer};

use crate::registry::{SchemaIds, frame};
use crate::{
    PostTradeConfig, PostTradeError, PostTradeEvent, Schemas, Stamper, TOPIC_ALLOCATIONS,
    TOPIC_CROSSES, TOPIC_ORDER_AUDIT, TOPIC_TRACKER_ANALYTICS, TOPIC_TRADES, topic_and_key,
};

/// Poll/backoff interval for the drain loop, matching
/// `d1-gateway-nats::DRAIN_POLL_INTERVAL`'s off-hot-path reasoning.
const POLL_INTERVAL: Duration = Duration::from_millis(10);
/// How long `flush` waits for in-flight sends to complete on shutdown.
const FLUSH_TIMEOUT: Duration = Duration::from_secs(5);
/// How long the startup topic-existence metadata fetch waits for the broker.
const METADATA_TIMEOUT: Duration = Duration::from_secs(10);

/// The two health signals `run_producer` writes for `d1.health`. Plain
/// atomics (not `d1-gateway-nats::PostTradeHealth`) so this crate stays free of
/// a gateway dependency; the caller lends the fields of whatever owns them.
/// Both are display-only, hence `Relaxed` (no other state is published
/// through them).
#[derive(Debug, Clone, Copy)]
pub struct ProducerFlags<'a> {
    /// Set once the schema registry accepted every schema (the drain loop is
    /// about to start).
    pub ready: &'a AtomicBool,
    /// Counts records dropped locally: `send()` errors, encode failures and
    /// unknown-topic drops.
    pub send_failures: &'a AtomicU64,
}

/// Run the Kafka producer until `shutdown` is set: verify the five
/// `posttrade.*` topics already exist, then drain `rx` to empty each poll,
/// encoding (via `stamper`) and publishing each event. Blocks the calling
/// thread -- spawn it from `crates/d1/src/lib.rs::spawn`, same shape as
/// `d1_gateway_nats::run_gateway`.
///
/// `shutdown` here is the producer's OWN flag (`d1::RunHandles::posttrade_shutdown`),
/// deliberately not shared with the core thread that pushes onto `rx`. The
/// caller's ordered-shutdown contract -- join the core thread, THEN flip
/// this flag -- is what makes the final drain below correct: by the time
/// this flag can observably be `true`, nothing can ever be pushed onto `rx`
/// again, so draining it to empty here really does pick up every event.
///
/// `run` is this process's `RunId`, stamped into every cross/allocation
/// `netting_cycle_id` (`"<run>/<n>"`) so the lineage key is restart-unique.
///
/// `flags` feed `d1.health` (see `ProducerFlags`).
pub fn run_producer(
    cfg: &PostTradeConfig,
    universe: Universe,
    mut rx: rtrb::Consumer<PostTradeEvent>,
    mut stamper: Stamper,
    shutdown: &AtomicBool,
    run: RunId,
    flags: ProducerFlags<'_>,
) -> Result<(), PostTradeError> {
    // ADR-002: at-least-once producer, idempotence + acks=all so a retried
    // send can never duplicate or silently lose a record.
    //
    // ponytail: plaintext broker connection (no `security.protocol`/SASL/TLS
    // config) -- matches `deploy/docker-compose.yml`'s single-broker
    // `PLAINTEXT`-only listener, a local-demo ceiling, not a stance that this
    // plane is plaintext-forever. Add `security.protocol=SASL_SSL` (+ the
    // matching broker-side listener) before this ever points at a
    // non-localhost broker.
    let producer: ThreadedProducer<DefaultProducerContext> = ClientConfig::new()
        .set("bootstrap.servers", &cfg.brokers)
        .set("enable.idempotence", "true")
        .set("acks", "all")
        .create()?;

    check_topics(&producer)?;

    let schemas = Schemas::new()?;
    // Register BEFORE the drain loop: once an event is popped off the ring it
    // is gone, so an id lookup that could fail mid-loop would mean silently
    // dropping booked records. Resolving all four up front makes every
    // subsequent `for_topic` infallible in practice.
    let schema_ids = SchemaIds::register(&cfg.registry_url)?;
    flags.ready.store(true, Ordering::Relaxed); // display-only, see `ProducerFlags`
    let drainer = Drainer {
        schemas: &schemas,
        schema_ids: &schema_ids,
        universe: &universe,
        producer: &producer,
        run,
        send_failures: flags.send_failures,
    };

    while !shutdown.load(Ordering::Relaxed) {
        if !drainer.drain(&mut rx, &mut stamper) {
            thread::sleep(POLL_INTERVAL);
        }
    }

    // One final pass after `shutdown` is observed. The loop above checks the
    // flag *before* draining, so anything pushed onto the ring while this
    // thread was asleep in `POLL_INTERVAL` is still queued when it exits --
    // and `flush` below only drains librdkafka's own queue (records already
    // handed to `send`), never this `rtrb` ring. Without this pass those
    // events would be silently lost, which on the post-trade plane means
    // booked trades missing from the compliance ledger. Thanks to the
    // caller's ordered-shutdown contract (this function's doc comment),
    // this pass is provably complete, not just a best-effort mop-up: `rx`
    // cannot receive anything new once `shutdown` is observably `true`.
    drainer.drain(&mut rx, &mut stamper);

    producer.flush(FLUSH_TIMEOUT)?;
    Ok(())
}

/// The read-only inputs `drain` needs besides the ring and the `Stamper`.
struct Drainer<'a> {
    schemas: &'a Schemas,
    schema_ids: &'a SchemaIds,
    universe: &'a Universe,
    producer: &'a ThreadedProducer<DefaultProducerContext>,
    run: RunId,
    send_failures: &'a AtomicU64,
}

impl Drainer<'_> {
    /// Drain `rx` to empty, encoding, framing and publishing each event. Returns
    /// whether any event was processed, so the caller knows whether to back off.
    fn drain(&self, rx: &mut rtrb::Consumer<PostTradeEvent>, stamper: &mut Stamper) -> bool {
        let mut did_work = false;

        while let Ok(event) = rx.pop() {
            did_work = true;
            let (topic, key) = topic_and_key(&event);
            // Resolved before encoding so a missing id costs no `Stamper` state:
            // `encode` mints ids/timestamps as a side effect, and burning them on
            // an event that is then dropped would put the golden-file sequence
            // out of step with the records actually published.
            let Some(schema_id) = self.schema_ids.for_topic(topic) else {
                self.send_failures.fetch_add(1, Ordering::Relaxed); // display-only
                eprintln!(
                    "d1-posttrade: {}, dropping event",
                    PostTradeError::UnknownTopic(topic.to_owned())
                );
                continue;
            };
            let datum = match self
                .schemas
                .encode(&event, self.universe, stamper, self.run)
            {
                Ok(datum) => datum,
                Err(err) => {
                    self.send_failures.fetch_add(1, Ordering::Relaxed); // display-only
                    eprintln!("d1-posttrade: encode failed, dropping event: {err}");
                    continue;
                }
            };
            // Confluent wire format (ADR-002): consumers resolve the writer
            // schema from this id rather than needing out-of-band knowledge.
            let bytes = frame(schema_id, &datum);
            // ponytail: log-and-drop on a local-queue-full send, same demo
            // ceiling as every `rtrb` ring in `crates/d1` -- a single demo
            // session, not a retry/backpressure protocol yet.
            if let Err((err, _record)) = self
                .producer
                .send(BaseRecord::to(topic).payload(&bytes).key(&key))
            {
                // ponytail: (N2) broker-side delivery failures are invisible --
                // `DefaultProducerContext` drops delivery reports, so this counts
                // only local drops (`send()` errors, encode failures, unknown topics). Upgrade: a counting `ProducerContext`.
                // Trigger: the first non-localhost broker (same as the plaintext
                // marker in `run_producer`). Owner undecided.
                self.send_failures.fetch_add(1, Ordering::Relaxed); // display-only
                eprintln!("d1-posttrade: send to {topic} failed, dropping: {err}");
            }
        }

        did_work
    }
}

/// Verify the five `posttrade.*` topics already exist on the broker before
/// this producer starts sending -- a hard startup error, not a warning, if
/// any are missing.
///
/// `deploy/docker-compose.yml` sets `KAFKA_AUTO_CREATE_TOPICS_ENABLE:
/// "false"`, so without this check a missing topic would mean every send to
/// it silently fails and gets dropped by this module's log-and-drop send
/// path above, with no signal at startup. Provisioning the topics is
/// `scripts/demo.sh`'s job (a later dispatch), not this producer's -- it
/// only verifies.
fn check_topics(producer: &ThreadedProducer<DefaultProducerContext>) -> Result<(), PostTradeError> {
    let metadata = producer.client().fetch_metadata(None, METADATA_TIMEOUT)?;

    let missing: Vec<&str> = [
        TOPIC_TRADES,
        TOPIC_CROSSES,
        TOPIC_ALLOCATIONS,
        TOPIC_ORDER_AUDIT,
        TOPIC_TRACKER_ANALYTICS,
    ]
    .into_iter()
    .filter(|&topic| !metadata.topics().iter().any(|t| t.name() == topic))
    .collect();

    if missing.is_empty() {
        Ok(())
    } else {
        Err(PostTradeError::MissingTopics(missing.join(", ")))
    }
}
