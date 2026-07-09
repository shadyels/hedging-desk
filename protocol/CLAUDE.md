# CLAUDE.md — protocol (schemas & wire governance)

Single source of truth for everything that crosses a process boundary.
If two components disagree about a message, this directory is right.

## Contents

- `proto/` — Protobuf schemas for the NATS live plane (EXO ↔ Delta One ↔ UI).
  Code generated for Rust (`prost`), Python (`protobuf`, picked —
  matches exo/pyproject.toml; do not introduce betterproto), TypeScript (`ts-proto`, `forceLong=bigint`). `just proto` regenerates
  all three; generated code is committed so builds don't need protoc.
- `avro/` — Avro schemas (`.avsc`) for the Kafka post-trade plane, registered
  in Schema Registry with `BACKWARD` compatibility mode.
- `nats-subjects.md` — the complete subject taxonomy. A subject not listed
  there does not exist.
- `refdata/` — instrument reference data for the demo universe (tick sizes,
  currencies, day counts, settlement lags). Defined once here, consumed by all
  components. Never duplicate a market convention in component code.

## Change rules (binding)

1. **Protobuf:** additive changes only. Never change a field number, never
   reuse a removed number (mark `reserved`), never change a field type. New
   optional fields are fine. Breaking change ⇒ new message/version + ADR.
2. **Avro:** must pass Schema Registry `BACKWARD` compatibility. New fields
   require defaults. `just schema-check` runs compatibility validation in CI.
3. **Subjects:** hierarchical, lowercase, dot-separated, documented with
   publisher, consumer(s), payload type, and cadence in `nats-subjects.md`.
   Wildcard subscriptions are allowed for consumers; publishers always publish
   to fully-qualified subjects.
4. **Units live in the schema, not in comments elsewhere:** `price_e9`
   (price × 10⁹ as i64/long), `qty_e2` (quantity × 10² as i64/long),
   timestamps as `uint64` nanoseconds since epoch, UTC. Field names carry the
   scale suffix so misuse is visible at the call site.
5. Every message includes the `Meta` block (`msg_id` UUIDv7, `producer`,
   `sent_ns`, `schema_version`).

## Demo universe (minimal during development; enriched at scale in P4.M5)

Defined in `refdata/universe.json`: a handful of large-cap equities (incl.
AAPL for the netting demo), one index + its future and a tracker, listed
options on one underlying, one FX pair (EURUSD) for TARF/PTARF, one govt bond
placeholder. Asset-class coverage (equities, indices, options, forwards,
futures, FX, bonds) is expressed in the *schema* from day one
(`InstrumentClass` enum) even though the populated universe is tiny.
