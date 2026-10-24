# NATS subject taxonomy (authoritative)

A subject not in this file does not exist. Publishers use fully-qualified subjects; consumers may use wildcards. All payloads are Protobuf messages from `protocol/proto/` and include the `Meta` block.

| Subject | Publisher | Consumers | Payload | Cadence |
|---|---|---|---|---|
| `exo.targets.<book>.<instrument>` | EXO (+ storyline harness `tracker_flow` stand-in for book 1, ADR-014 §10) | Delta One, UI | `TargetPosition` | on rehedge decision (band-gated, ADR-014 §9) |
| `exo.valuations.<book>` | EXO | **Delta One (Tier-1 limits)**, UI | `ValuationSnapshot` | per revaluation + republish every `revaluation_interval_s` |
| `exo.alerts.recon` | EXO | UI | `ReconAlert` | on halt engaged / cleared (ADR-014 §8) |
| `d1.exec.<book>.<instrument>` | Delta One | EXO, UI | `ExecutionReport` | per order event; book 0 = netting parent, book N = per-book allocation (ADR-014 §5) |
| **`d1.marks.<instrument>`** (new) | Delta One | EXO, UI | `MarkPrice` | on mid change, ≥ 250 ms per instrument (trailing edge) + 1 s keepalive (ADR-014 §2) |
| `d1.positions.<book>` | Delta One | EXO, UI | `PositionSnapshot` | every 1 s for every book + on change (ADR-014 §7) |
| `d1.risk.firm` | Delta One | UI | `RiskSnapshot` | periodic (1 s) — **not yet published; P3.M1** |
| `d1.alerts.limits` | Delta One | UI, EXO | `RiskLimitAlert` | on level change: warn / hard / cleared (ADR-014 §3) |
| `d1.crosses` | Delta One | **EXO**, UI | `InternalCrossNotice` (live notice; booked record is on Kafka) | per cross |
| `exo.proposals.<book>` | EXO | Delta One (caches active), UI | `HedgeProposal` | on Tier-2 trigger (breach or trader request) |
| `d1.cmd.proposal` | UI (request) | Delta One (reply) | `CommandRequest.ProposalDecision` → `CommandAck` | trader approve/reject |
| `exo.transfers.<book>` | EXO | Delta One | `InternalTransferRequest` | on rho-transfer trigger (ADR-009) |
| `d1.cmd.order` | UI (request) | Delta One (reply) | `CommandRequest` → `CommandAck` | manual, Phase 3 |
| `d1.cmd.killswitch` | UI (request) | Delta One (reply) | `CommandRequest` → `CommandAck` | manual, Phase 3 |
| `d1.tracker.<book>` | Delta One | UI | `TrackerAnalytics` | per sampling interval (ADR-010) |
| `d1.health` | Delta One | UI | `HealthStatus` | 1s heartbeat |
| `exo.health` | EXO | UI | `HealthStatus` | 1s heartbeat |
| `sim.md.<instrument>` | sim | (Delta One ingests via its feed boundary, not via NATS — this subject exists only for UI display of the simulated tape) | `Tick` | streaming |

Naming rules: lowercase, dot-separated, `<book>`/`<instrument>` tokens are the numeric canonical ids (`book_id`, `instrument_id`) from `refdata/universe.json` — never codes or symbols (ADR-014 §1). Adding a subject requires updating this table in the same PR.
