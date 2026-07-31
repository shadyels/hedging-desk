//! Kafka producer thread (P1.M4 Slice 2): drains the `posttrade` `rtrb` ring,
//! encodes each event via `Schemas::encode` (raw Avro datum, no Confluent
//! magic-byte framing), and publishes it to its `posttrade.*` topic keyed per
//! `topic_and_key` (ADR-002). Non-async, off the hot path -- mirrors
//! `d1-gateway-nats::run_gateway`'s poll/drain-loop shape, `rdkafka`'s
//! `ThreadedProducer` instead of `async-nats`.

use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::Duration;

use d1_refdata::Universe;
use rdkafka::ClientConfig;
use rdkafka::producer::{BaseRecord, DefaultProducerContext, Producer, ThreadedProducer};

use crate::{
    PostTradeError, PostTradeEvent, Schemas, Stamper, TOPIC_ALLOCATIONS, TOPIC_CROSSES,
    TOPIC_ORDER_AUDIT, TOPIC_TRADES, topic_and_key,
};

/// Poll/backoff interval for the drain loop, matching
/// `d1-gateway-nats::DRAIN_POLL_INTERVAL`'s off-hot-path reasoning.
const POLL_INTERVAL: Duration = Duration::from_millis(10);
/// How long `flush` waits for in-flight sends to complete on shutdown.
const FLUSH_TIMEOUT: Duration = Duration::from_secs(5);
/// How long the startup topic-existence metadata fetch waits for the broker.
const METADATA_TIMEOUT: Duration = Duration::from_secs(10);

/// Run the Kafka producer until `shutdown` is set: verify the four
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
pub fn run_producer(
    brokers: &str,
    universe: Universe,
    mut rx: rtrb::Consumer<PostTradeEvent>,
    mut stamper: Stamper,
    shutdown: &AtomicBool,
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
        .set("bootstrap.servers", brokers)
        .set("enable.idempotence", "true")
        .set("acks", "all")
        .create()?;

    check_topics(&producer)?;

    let schemas = Schemas::new()?;

    while !shutdown.load(Ordering::Relaxed) {
        if !drain(&mut rx, &schemas, &universe, &mut stamper, &producer) {
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
    drain(&mut rx, &schemas, &universe, &mut stamper, &producer);

    producer.flush(FLUSH_TIMEOUT)?;
    Ok(())
}

/// Drain `rx` to empty, encoding and publishing each event. Returns whether
/// any event was processed, so the caller knows whether to back off.
fn drain(
    rx: &mut rtrb::Consumer<PostTradeEvent>,
    schemas: &Schemas,
    universe: &Universe,
    stamper: &mut Stamper,
    producer: &ThreadedProducer<DefaultProducerContext>,
) -> bool {
    let mut did_work = false;

    while let Ok(event) = rx.pop() {
        did_work = true;
        let bytes = match schemas.encode(&event, universe, stamper) {
            Ok(bytes) => bytes,
            Err(err) => {
                eprintln!("d1-posttrade: encode failed, dropping event: {err}");
                continue;
            }
        };
        let (topic, key) = topic_and_key(&event);
        // ponytail: log-and-drop on a local-queue-full send, same demo
        // ceiling as every `rtrb` ring in `crates/d1` -- a single demo
        // session, not a retry/backpressure protocol yet.
        if let Err((err, _record)) = producer.send(BaseRecord::to(topic).payload(&bytes).key(&key))
        {
            eprintln!("d1-posttrade: send to {topic} failed, dropping: {err}");
        }
    }

    did_work
}

/// Verify the four `posttrade.*` topics already exist on the broker before
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
