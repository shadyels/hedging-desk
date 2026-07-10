# ADR-001: NATS core for the live plane (EXO ↔ Delta One ↔ UI)

**Status:** Accepted
**Date:** 2026-07-05
**Deciders:** desk lead

## Context

EXO publishes target positions and consumes executions/risk; the UI consumes live state and issues occasional commands. Candidate transports: Aeron, NATS, gRPC streaming, Kafka. Constraint that dominates the choice: our endpoints are **Rust and Python** (and TypeScript in a browser), and the bus is **not** on the 10–50 µs tick-to-trade path (that path is entirely in-process inside Delta One — see ADR-004). Cadence on this bus is per-rebalance/per-fill, i.e. milliseconds-to-seconds, thousands of msgs/sec at most.

## Options considered

### A: Aeron
| Dimension | Assessment |
|---|---|
| Latency | Best-in-class (shared memory/UDP), the HFT reference choice |
| Rust client | Third-party only: `aeron-rs` (a port; per its own docs requires building the C media driver separately — UnitedTraders, n.d., https://github.com/UnitedTraders/aeron-rs) and `rusteron` (FFI wrapper over the C API; self-described production-ready but explicitly unsafe-FFI-based with segfault risk on misuse — mimran1980, n.d., https://github.com/mimran1980/rusteron; lib.rs, 2026-03-10, https://lib.rs/crates/rusteron-client) |
| Python client | Weaker still; not a first-class Aeron target |
| Ops | Media driver per host to run, tune, monitor |
| Browser/UI | No story; would need a separate gateway |

**Pros:** unmatched latency headroom; strong finance pedigree. **Cons:** its advantage sits exactly where we don't need it; client maturity risk in *both* our languages; extra ops component; no UI path.

### B: NATS core
| Dimension | Assessment |
|---|---|
| Latency | Sub-millisecond; NATS's own bench tooling shows ~51 µs average request-reply between two bench processes in their documented example (NATS docs, n.d., https://docs.nats.io/using-nats/nats-tools/nats_cli/natsbench); a 2026 third-party benchmark reports <100 µs at high load (dasroot.net, 2026-03-04, https://dasroot.net/posts/2026/03/message-brokers-ai-kafka-nats-rabbitmq/) — orders of magnitude more headroom than this plane needs |
| Rust client | First-party `async-nats` maintained in the nats-io org |
| Python client | First-party `nats-py` |
| Ops | Single small binary; clustering later if needed |
| Browser/UI | Native WebSocket support → UI subscribes directly via `nats.ws` |
| Extras | Request/reply built-in (UI commands); JetStream available if we later want replayable target streams |

**Pros:** first-party clients in all three of our languages; simplest ops; request/reply + pub/sub + WS in one system; trajectory is "on the route to standard" for cloud-native messaging. **Cons:** brokered hop (~tens of µs–ms) — irrelevant here by design; less finance-specific pedigree than Aeron.

### C: gRPC streaming
Universal but point-to-point: we'd hand-build pub/sub fan-out, reconnect, and UI fan-out that NATS gives for free. Higher latency than either alternative. Rejected.

### D: Kafka for the live plane
Wrong tool: ms-level latency, heavy clients, partition semantics we don't
want for live targets. It is the right tool for post-trade (ADR-002).

## Decision

**NATS core** for the live plane. Protobuf payloads (ADR-004 §wire), subjects
per `protocol/nats-subjects.md`. JetStream not enabled in Phase 1/2; revisit
if replayable target history is demanded (ADR to supersede).

## Post-decision context (2026-07-06)

Desk confirmed: no in-house C++ libraries or C++ expertise. This removes the
one scenario under which a C++ (+Aeron) engine would have been defensible;
Rust + NATS reaffirmed. Aeron re-enters consideration only if a hot
inter-process wire ever appears (would supersede this ADR).

## Consequences

- Easier: one bus serves EXO, Delta One and the browser UI with first-party
  clients; demo ops is `docker compose up`.
- Harder: if the bus ever *does* need single-digit-µs delivery (it shouldn't — that would mean hot-path logic leaked out of Delta One), we would revisit Aeron; the Protobuf-over-subjects abstraction keeps that swap contained in the gateway crates/packages.
- We accept dependence on third-party benchmark figures above as indicative only; `just bench-bus` measures our actual deployment and the demo quotes our own numbers, not vendors'.
