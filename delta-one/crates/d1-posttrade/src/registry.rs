//! Confluent Schema Registry integration (ADR-002): register the five
//! post-trade Avro schemas at producer startup, cache the ids the registry
//! assigns, and wrap each encoded datum in the Confluent wire format.
//!
//! P1.M4 Slices 1–2 published **raw** Avro datums, which meant a payload was
//! only decodable by a consumer that already knew which schema wrote it —
//! defeating the replayability ADR-002 chose this plane for. Framing closes
//! that: every record now carries the id of the exact writer schema, so a
//! ledger-ingestion or compliance-replay consumer can resolve it from the
//! registry, including across a schema evolution.
//!
//! Off the hot path (crate table in `delta-one/CLAUDE.md`): `String`, heap
//! allocation and blocking HTTP are all fine here. The five registration
//! calls happen once, at producer-thread startup, before the drain loop.

use crate::{
    PostTradeError, TOPIC_ALLOCATIONS, TOPIC_CROSSES, TOPIC_ORDER_AUDIT, TOPIC_TRACKER_ANALYTICS,
    TOPIC_TRADES,
    convert::{ALLOCATION_AVSC, CROSS_AVSC, ORDER_AUDIT_AVSC, TRACKER_ANALYTICS_AVSC, TRADE_AVSC},
};

/// Confluent wire-format magic byte: the first byte of every framed payload,
/// identifying format version 0 (the only one Confluent has defined).
pub const MAGIC_BYTE: u8 = 0x00;

/// Length of the Confluent frame prefix: 1 magic byte + a 4-byte big-endian
/// schema id.
pub const FRAME_LEN: usize = 5;

/// Content type the Schema Registry expects on write requests. Matches what
/// `scripts/schema-check.sh` already sends, so both paths speak to the
/// registry identically.
const REGISTRY_CONTENT_TYPE: &str = "application/vnd.schemaregistry.v1+json";

/// Wrap one encoded Avro datum in the Confluent wire format:
/// `0x00 || schema_id (4 bytes, big-endian) || datum`.
///
/// This is the *only* place the frame layout is written; `golden_posttrade`'s
/// decode path is the only place it is read.
#[must_use]
pub fn frame(schema_id: u32, datum: &[u8]) -> Vec<u8> {
    let mut framed = Vec::with_capacity(FRAME_LEN + datum.len());
    framed.push(MAGIC_BYTE);
    framed.extend_from_slice(&schema_id.to_be_bytes());
    framed.extend_from_slice(datum);
    framed
}

/// Schema Registry ids for the five `posttrade.*` subjects, resolved once at
/// producer startup by [`SchemaIds::register`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SchemaIds {
    trade: u32,
    cross: u32,
    allocation: u32,
    order_audit: u32,
    tracker: u32,
}

impl SchemaIds {
    /// Register all five post-trade schemas with the registry at
    /// `registry_url` and return the ids it assigned.
    ///
    /// Registration is idempotent by the registry's own contract: POSTing a
    /// schema identical to one already registered under that subject returns
    /// the **existing** id rather than creating a new version. That is what
    /// makes this safe to run on every process start, and it is the same
    /// property `scripts/schema-check.sh` relies on when it registers the
    /// `main`-branch baseline.
    ///
    /// The registered bytes cannot drift from the bytes the encoder writes
    /// against: both come from the same `convert::*_AVSC` consts that
    /// [`Schemas::new`] parses.
    ///
    /// Errors are hard: a caller that cannot resolve ids must not fall back
    /// to publishing unframed records, which is precisely the state this
    /// module exists to eliminate.
    pub fn register(registry_url: &str) -> Result<Self, PostTradeError> {
        Ok(Self {
            trade: register_subject(registry_url, TOPIC_TRADES, TRADE_AVSC)?,
            cross: register_subject(registry_url, TOPIC_CROSSES, CROSS_AVSC)?,
            allocation: register_subject(registry_url, TOPIC_ALLOCATIONS, ALLOCATION_AVSC)?,
            order_audit: register_subject(registry_url, TOPIC_ORDER_AUDIT, ORDER_AUDIT_AVSC)?,
            tracker: register_subject(
                registry_url,
                TOPIC_TRACKER_ANALYTICS,
                TRACKER_ANALYTICS_AVSC,
            )?,
        })
    }

    /// The registered schema id for `topic`.
    ///
    /// Returns `None` for any topic outside the five `posttrade.*` subjects;
    /// the producer treats that as an encode failure rather than guessing an
    /// id, since a wrong id produces a record that decodes to garbage.
    #[must_use]
    pub fn for_topic(&self, topic: &str) -> Option<u32> {
        match topic {
            TOPIC_TRADES => Some(self.trade),
            TOPIC_CROSSES => Some(self.cross),
            TOPIC_ALLOCATIONS => Some(self.allocation),
            TOPIC_ORDER_AUDIT => Some(self.order_audit),
            TOPIC_TRACKER_ANALYTICS => Some(self.tracker),
            _ => None,
        }
    }
}

/// Subject name for a topic under the registry's default `TopicNameStrategy`
/// (`<topic>-value`) — the same mapping `scripts/schema-check.sh` uses, so
/// the CI compatibility gate and this producer check the same subjects.
fn subject_for(topic: &str) -> String {
    format!("{topic}-value")
}

/// POST one schema to `/subjects/<topic>-value/versions` and return the id
/// the registry assigned.
fn register_subject(registry_url: &str, topic: &str, avsc: &str) -> Result<u32, PostTradeError> {
    let subject = subject_for(topic);
    let url = format!("{registry_url}/subjects/{subject}/versions");

    // The registry takes the schema as a JSON *string* field, so the .avsc
    // document is serialized into a string rather than embedded as an
    // object. `serde_json` does the escaping; hand-rolling it would break on
    // the first schema doc containing a quote.
    let body = serde_json::json!({ "schema": avsc }).to_string();

    // `ureq`'s `http_status_as_error` defaults to true, so a 4xx/5xx from the
    // registry (e.g. an incompatible schema rejected under BACKWARD) arrives
    // here as `Error::StatusCode`, not as an Ok response to be inspected.
    let mut response = ureq::post(&url)
        .header("Content-Type", REGISTRY_CONTENT_TYPE)
        .send(&body)
        .map_err(|source| PostTradeError::SchemaRegistry {
            subject: subject.clone(),
            url: url.clone(),
            source: Box::new(source),
        })?;

    let parsed: serde_json::Value =
        response
            .body_mut()
            .read_json()
            .map_err(|source| PostTradeError::SchemaRegistry {
                subject: subject.clone(),
                url: url.clone(),
                source: Box::new(source),
            })?;

    let id = parsed
        .get("id")
        .and_then(serde_json::Value::as_u64)
        .ok_or_else(|| PostTradeError::SchemaRegistryResponse {
            subject: subject.clone(),
            body: parsed.to_string(),
        })?;

    u32::try_from(id).map_err(|_| PostTradeError::SchemaRegistryResponse {
        subject,
        body: format!("schema id {id} does not fit in the wire format's u32"),
    })
}

#[cfg(test)]
#[allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::indexing_slicing
)] // tests: hot-path-only bans (delta-one/CLAUDE.md)
mod tests {
    use super::*;

    #[test]
    fn frame_prepends_magic_byte_and_big_endian_id() {
        let framed = frame(1, &[0xAA, 0xBB]);
        assert_eq!(framed, vec![0x00, 0x00, 0x00, 0x00, 0x01, 0xAA, 0xBB]);
    }

    #[test]
    fn frame_encodes_large_ids_big_endian() {
        // 0x01020304 must land most-significant-byte first, not native order
        // -- a little-endian slip decodes as a different (or absent) schema.
        let framed = frame(0x0102_0304, &[]);
        assert_eq!(framed, vec![0x00, 0x01, 0x02, 0x03, 0x04]);
    }

    #[test]
    fn frame_leaves_datum_byte_identical() {
        let datum: Vec<u8> = (0u8..=255).collect();
        let framed = frame(42, &datum);
        assert_eq!(framed.len(), FRAME_LEN + datum.len());
        assert_eq!(&framed[FRAME_LEN..], &datum[..]);
    }

    #[test]
    fn frame_round_trips_through_a_strip() {
        let datum = b"avro-datum-bytes";
        let framed = frame(7, datum);
        let (prefix, payload) = framed.split_at(FRAME_LEN);
        assert_eq!(prefix[0], MAGIC_BYTE);
        assert_eq!(
            u32::from_be_bytes([prefix[1], prefix[2], prefix[3], prefix[4]]),
            7
        );
        assert_eq!(payload, datum);
    }

    #[test]
    fn subject_uses_topic_name_strategy() {
        assert_eq!(subject_for(TOPIC_TRADES), "posttrade.trades-value");
        assert_eq!(
            subject_for(TOPIC_ORDER_AUDIT),
            "posttrade.orders.audit-value"
        );
    }

    #[test]
    fn for_topic_covers_the_five_subjects_and_rejects_others() {
        let ids = SchemaIds {
            trade: 1,
            cross: 2,
            allocation: 3,
            order_audit: 4,
            tracker: 5,
        };
        assert_eq!(ids.for_topic(TOPIC_TRADES), Some(1));
        assert_eq!(ids.for_topic(TOPIC_CROSSES), Some(2));
        assert_eq!(ids.for_topic(TOPIC_ALLOCATIONS), Some(3));
        assert_eq!(ids.for_topic(TOPIC_ORDER_AUDIT), Some(4));
        // The tripwire this milestone lands: `posttrade.tracker.analytics`
        // now resolves instead of falling through to `None` (P1.M5 Slice 3).
        assert_eq!(
            ids.for_topic(TOPIC_TRACKER_ANALYTICS),
            Some(5),
            "posttrade.tracker.analytics must resolve now that P1.M5 Slice 3 has landed"
        );
        // A genuinely unknown subject still falls through.
        assert_eq!(ids.for_topic("posttrade.nonexistent"), None);
    }
}
