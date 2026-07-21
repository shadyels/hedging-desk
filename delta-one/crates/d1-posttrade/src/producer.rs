//! Kafka producer thread (P1.M4 Slice 2): drains the `posttrade` `rtrb` ring,
//! encodes each event via `Schemas::encode` (raw Avro datum, no Confluent
//! magic-byte framing), and publishes it to its `posttrade.*` topic keyed per
//! `topic_and_key` (ADR-002). Non-async, off the hot path -- mirrors
//! `d1-gateway-nats::run_gateway`'s poll/drain-loop shape, `rdkafka`'s
//! `ThreadedProducer` instead of `async-nats`.

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::task::{Context, Poll, Wake, Waker};
use std::thread;
use std::time::Duration;

use d1_refdata::Universe;
use rdkafka::ClientConfig;
use rdkafka::admin::{AdminClient, AdminOptions, NewTopic, TopicReplication};
use rdkafka::client::DefaultClientContext;
use rdkafka::error::{KafkaError, RDKafkaErrorCode};
use rdkafka::producer::{BaseRecord, DefaultProducerContext, Producer, ThreadedProducer};

use crate::{
    PostTradeError, PostTradeEvent, Schemas, TOPIC_ALLOCATIONS, TOPIC_CROSSES, TOPIC_ORDER_AUDIT,
    TOPIC_TRADES, topic_and_key,
};

/// Poll/backoff interval for the drain loop, matching
/// `d1-gateway-nats::DRAIN_POLL_INTERVAL`'s off-hot-path reasoning.
const POLL_INTERVAL: Duration = Duration::from_millis(10);
/// How long `flush` waits for in-flight sends to complete on shutdown.
const FLUSH_TIMEOUT: Duration = Duration::from_secs(5);
/// How long topic creation waits for the broker to confirm.
const TOPIC_CREATE_TIMEOUT: Duration = Duration::from_secs(10);
/// Demo-sized single-broker compose (`deploy/docker-compose.yml`:
/// `KAFKA_NODE_ID: 1`) -- one partition, replication factor 1 for every
/// `posttrade.*` topic.
const NEW_TOPIC_PARTITIONS: i32 = 1;
const NEW_TOPIC_REPLICATION: i32 = 1;

/// Run the Kafka producer until `shutdown` is set: ensure the four
/// `posttrade.*` topics exist, then drain `rx` to empty each poll, encoding
/// and publishing each event. Blocks the calling thread -- spawn it from
/// `crates/d1/src/lib.rs::spawn`, same shape as `d1_gateway_nats::run_gateway`.
pub fn run_producer(
    brokers: &str,
    universe: Universe,
    mut rx: rtrb::Consumer<PostTradeEvent>,
    shutdown: &AtomicBool,
) -> Result<(), PostTradeError> {
    ensure_topics(brokers)?;

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

    let schemas = Schemas::new()?;

    while !shutdown.load(Ordering::Relaxed) {
        let mut did_work = false;

        while let Ok(event) = rx.pop() {
            did_work = true;
            let bytes = match schemas.encode(&event, &universe) {
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
            if let Err((err, _record)) =
                producer.send(BaseRecord::to(topic).payload(&bytes).key(&key))
            {
                eprintln!("d1-posttrade: send to {topic} failed, dropping: {err}");
            }
        }

        if !did_work {
            thread::sleep(POLL_INTERVAL);
        }
    }

    producer.flush(FLUSH_TIMEOUT)?;
    Ok(())
}

/// Create the four `posttrade.*` topics if they don't already exist.
///
// ponytail: in-process topic-ensure on every producer startup (idempotent --
// a `TopicAlreadyExists` reply is expected and swallowed on every restart
// after the first), not a deploy-time provisioning step. Compose sets
// `KAFKA_AUTO_CREATE_TOPICS_ENABLE=false` (`deploy/docker-compose.yml`), so
// something has to create these explicitly; promote to a `scripts/demo.sh`
// (or real deploy-time) provisioning step once topic config (partitions,
// replication, retention) needs managing outside this code.
fn ensure_topics(brokers: &str) -> Result<(), PostTradeError> {
    let admin: AdminClient<DefaultClientContext> = ClientConfig::new()
        .set("bootstrap.servers", brokers)
        .create()?;

    let topic_names = [
        TOPIC_TRADES,
        TOPIC_CROSSES,
        TOPIC_ALLOCATIONS,
        TOPIC_ORDER_AUDIT,
    ];
    let new_topics = topic_names.map(|name| {
        NewTopic::new(
            name,
            NEW_TOPIC_PARTITIONS,
            TopicReplication::Fixed(NEW_TOPIC_REPLICATION),
        )
    });
    let opts = AdminOptions::new().operation_timeout(Some(TOPIC_CREATE_TIMEOUT));

    let results = block_on(admin.create_topics(&new_topics, &opts))?;
    for result in results {
        if let Err((topic, err)) = result {
            if err != RDKafkaErrorCode::TopicAlreadyExists {
                return Err(PostTradeError::Kafka(KafkaError::AdminOp(err)));
            }
            println!("d1-posttrade: topic {topic} already exists, continuing");
        }
    }
    Ok(())
}

/// Minimal std-only executor to drive `AdminClient::create_topics`'s `Future`
/// to completion without pulling in a full async runtime (`tokio`/`futures`
/// aren't on this crate's approved dependency table, and `rdkafka`'s
/// `AdminClient` already runs its own background thread polling the native
/// event queue, so all this needs to do is park/unpark the calling thread
/// when that background thread wakes the task).
fn block_on<F: Future>(fut: F) -> F::Output {
    struct ThreadWaker(thread::Thread);
    impl Wake for ThreadWaker {
        fn wake(self: Arc<Self>) {
            self.0.unpark();
        }
    }

    let waker = Waker::from(Arc::new(ThreadWaker(thread::current())));
    let mut cx = Context::from_waker(&waker);
    let mut fut: Pin<Box<F>> = Box::pin(fut);
    loop {
        match fut.as_mut().poll(&mut cx) {
            Poll::Ready(output) => return output,
            Poll::Pending => thread::park(),
        }
    }
}
