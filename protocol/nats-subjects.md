# NATS subject taxonomy (authoritative)

A subject not in this file does not exist. Publishers use fully-qualified subjects; consumers may use wildcards. All payloads are Protobuf messages from `protocol/proto/` and include the `Meta` block.

| Subject | Publisher | Consumers | Payload | Cadence |
|---|---|---|---|---|
| `exo.targets.<book>.<instrument>` | EXO | Delta One, UI | `TargetPosition` | on rehedge decision (event-driven, seconds-scale) |
| `exo.valuations.<book>` | EXO | UI | `ValuationSnapshot` | per revaluation cycle |
| `exo.alerts.recon` | EXO | UI, Delta One | `ReconAlert` | on divergence |
| `d1.exec.<book>.<instrument>` | Delta One | EXO, UI | `ExecutionReport` | per order event |
| `d1.positions.<book>` | Delta One | EXO, UI | `PositionSnapshot` | periodic (1s) + on change |
| `d1.risk.firm` | Delta One | UI | `RiskSnapshot` | periodic (1s) |
| `d1.alerts.limits` | Delta One | UI, EXO | `RiskLimitAlert` | on warning/breach (Tier-1, ADR-008) |
| `d1.crosses` | Delta One | UI | `InternalCrossNotice` (live notice; booked record is on Kafka) | per cross |
| `exo.proposals.<book>` | EXO | Delta One (caches active), UI | `HedgeProposal` | on Tier-2 trigger (breach or trader request) |
| `d1.cmd.proposal` | UI (request) | Delta One (reply) | `CommandRequest.ProposalDecision` → `CommandAck` | trader approve/reject |
| `exo.transfers.<book>` | EXO | Delta One | `InternalTransferRequest` | on rho-transfer trigger (ADR-009) |
| `d1.cmd.order` | UI (request) | Delta One (reply) | `CommandRequest` → `CommandAck` | manual, Phase 3 |
| `d1.cmd.killswitch` | UI (request) | Delta One (reply) | `CommandRequest` → `CommandAck` | manual, Phase 3 |
| `d1.tracker.<book>` | Delta One | UI | `TrackerAnalytics` | per sampling interval (ADR-010) |
| `d1.health` | Delta One | UI | `HealthStatus` | 1s heartbeat |
| `exo.health` | EXO | UI | `HealthStatus` | 1s heartbeat |
| `sim.md.<instrument>` | sim | (Delta One ingests via its feed boundary, not via NATS — this subject exists only for UI display of the simulated tape) | `Tick` | streaming |

Naming rules: lowercase, dot-separated, `<book>`/`<instrument>` tokens are the canonical ids from `refdata/universe.json`. Adding a subject requires updating this table in the same PR.
