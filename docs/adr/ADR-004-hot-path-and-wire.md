# ADR-004: Hot-path boundary and serialization choices

**Status:** Accepted
**Date:** 2026-07-05
**Deciders:** desk lead (10–50 µs T2T tier confirmed)

## Context

The stated target is 10–50 µs tick-to-trade. A brokered network hop (NATS) or any serialization on that path would consume the budget. The target is achievable only if the path is: feed ingest → position/risk check → netting → order emit, all inside the Delta One process, lock-free, allocation-free (analysis/design constraint, not a cited fact — we prove it with our own HDR-histogram benchmarks; no external latency claims are quoted for our system).

## Decision

1. **Hot path is in-process only.** EXO targets, UI commands, Kafka, and even the NATS publish of execution reports are all *off* the path, connected via bounded SPSC rings. An EXO target updates the desired-position state; the next tick (or an immediate synthetic evaluation event) drives execution. Consequence stated plainly for the demo narrative: the 10–50 µs figure is quoted for tick→order-emit inside Delta One; EXO-target→order is milliseconds and that is correct system design, not a shortcut.
2. **Wire format on NATS: Protobuf** (first-party codegen in Rust/Python/TS, compact, evolvable). SBE was considered — it is the HFT-grade choice — but its advantage matters on hot wires, and per (1) we have none between processes. Revisit only if a hot inter-process wire ever appears.
3. **Wire format on Kafka: Avro** (ADR-002).
4. **In-process representation:** plain fixed-size Rust structs; Protobuf
   types never cross into `d1-core`/`d1-netting` (gateway converts at the
   edge).
5. **Measurement is a deliverable:** `just bench` produces p50/p99/p99.9
   tick-to-order-emit HDR histograms on release builds with pinned cores;
   these numbers, and only these, go in the showcase deck.

## Consequences

- Easier: latency engineering is confined to one process; buses can be boring.
- Harder: Delta One carries a strict internal discipline (see `delta-one/CLAUDE.md` hot-path contract); double conversion (proto ↔ internal structs) at gateways — deliberate cost.
