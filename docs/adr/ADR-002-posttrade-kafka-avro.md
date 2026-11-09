# ADR-002: Kafka + Avro + Schema Registry for the post-trade plane

**Status:** Accepted
**Date:** 2026-07-05
**Deciders:** desk lead (Kafka confirmed over RabbitMQ)

## Context

Delta One must publish booked trades, explicit internal crosses, allocations and an order audit trail to a plane where latency is irrelevant and replayability, ordering-per-key, retention and schema evolution are the point. Downstream: post-trade ledger ingestion, compliance replay, T+0 recon.

## Options considered

- **Kafka (Avro + Schema Registry):** durable replayable log, per-key ordering (`instrument_id` for trades, `cross_id` for crosses), compaction available for latest-state topics, BACKWARD-compatible schema evolution enforced centrally, idempotent producer. Market-standard for exactly this pipeline.
- **RabbitMQ:** queue semantics, message deleted on consume; replaying a ledger history is not its model. Rejected for this plane.
- **JSON on Kafka:** no enforced schema, silent field drift, larger payloads. Rejected — Avro chosen per root invariant #3.

## Decision

Kafka (KRaft mode, no ZooKeeper) + Avro + Confluent Schema Registry with `BACKWARD` compatibility. Topics: `posttrade.trades`, `posttrade.crosses`, `posttrade.allocations`, `posttrade.orders.audit`. Producer: `enable.idempotence=true`, `acks=all`. Consumers dedupe on `msg_id` (at-least-once end-to-end).

## Consequences

- Easier: ledger ingestion, compliance replay, golden-file end-to-end tests (`sim/` diffs deterministic scenario output against Kafka history).
- Harder: one more moving part in compose (registry); schema changes gated by compatibility checks — that friction is intentional.

## Amendment 1 (P3.M1 Slice 2): `netting_cycle_id` value format

**Status:** Proposed · **Date:** 2026-11-09

`posttrade.crosses.netting_cycle_id` and `posttrade.allocations.netting_cycle_id` keep their name, position and `string` type. Their value changes from a bare per-process counter (`"2"`), which collides across Delta One restarts, to `"<run_uuid>/<n>"`, with `"DIRECT"` unchanged for directed transfers (ADR-014 Amendment 1 §4). Only the schemas' `doc` attribute changes, which is BACKWARD-compatible. Records produced before this change hold a bare `n` and cannot be attributed to a run. Consumers must treat the field as opaque. Golden fixtures are regenerated for exactly this value change.
