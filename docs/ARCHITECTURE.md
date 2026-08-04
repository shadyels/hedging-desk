# Architecture

Read together with the ADRs in `docs/adr/` (binding) and the per-component CLAUDE.md files.

## Components and planes

```
                          LIVE PLANE (NATS core, Protobuf)
        exo.targets.*  ┌──────────────────────────────────┐  d1.exec.*, d1.risk.*
   ┌───────────┐  ────▶│                                  │────▶  ┌────────┐
   │    EXO    │       │            NATS server           │       │   UI   │
   │  (Python) │  ◀────│        (WS listener for UI)      │◀────  │  (TS)  │
   └───────────┘       └──────────────▲───────────────────┘  d1.cmd.* (req/reply)
        ▲                             │
        │ fills, risk, recon          │
        │                     ┌───────┴────────┐
   valuation loop             │   DELTA ONE    │
   (seconds cadence)          │     (Rust)     │
                              │ ┌────────────┐ │   EXECUTION PLANE
   sim market data ══════════▶│ │  HOT PATH  │ │──FIX 4.4──▶ sim FIX acceptor
   (ingest boundary included  │ │ ingest→risk│ │
    in latency measurement)   │ │ →net→emit  │ │   POST-TRADE PLANE
                              │ └────────────┘ │──Kafka(Avro)─▶ posttrade.*
                              └────────────────┘
```

- **Live plane:** NATS core pub/sub + request/reply (ADR-001). Not latency critical by design (ADR-004).
- **Execution plane:** FIX 4.4 via QuickFIX binding (ADR-003), demo counterparty in `sim/`.
- **Post-trade plane:** Kafka + Avro + Schema Registry (ADR-002).

## Delta One internal structure

Pinned threads connected by bounded SPSC rings; gateways on separate runtimes:

```
[feed ingest] → [position/risk] → [netting] → [order emit] → (ring) → FIX gateway
      │                │                                        │
      └──(rings, off-path)──▶ NATS gateway (targets in, exec/risk out)
                                        └──▶ Kafka producer (post-trade)
```

EXO targets arrive via the NATS gateway and update desired-position state; execution decisions happen on the hot path at the next evaluation event.

## Latency budget

Design targets (our own analysis; to be replaced by measured HDR-histogram numbers from `just bench` — no external latency figures are quoted for this system):

| Stage (in-process) | Budget p99 |
|---|---|
| Feed decode + book update | ≤ 10 µs |
| Risk checks (limits, fat-finger, band) | ≤ 5 µs |
| Netting evaluation (incremental) | ≤ 10 µs |
| Order build + emit to FIX ring | ≤ 10 µs |
| **Tick → order-emit total** | **≤ 50 µs p99, ≤ 20 µs p50 target** |

The **Tick → order-emit total** row is now backed by a measurement rather than analysis: `delta-one/crates/d1/benches/tick_to_trade.rs` composes the real ADR-004 §1 sequence in a single timed closure and reports p50/p99/p99.9 (see `docs/ROADMAP.md`'s Phase 1 exit note for the current numbers and the two limitations — unpinned cores, and a composed rather than live-loop-causal trigger). The four per-stage rows above it remain **design targets**: the benches that cover them (`crates/d1-core/benches/hot_path.rs`, `crates/d1-netting/benches/netting.rs`) measure each stage in isolation, which cannot account for the queueing and cache effects between stages.

Off-path (informational, not part of the T2T claim): EXO full-book revaluation cadence in seconds; NATS delivery sub-millisecond; Kafka end-to-end milliseconds.

## What is hedged vs what is monitored (Greeks policy — ADR-008)

Per-Greek treatment ladder (full rationale and sources in ADR-008): **delta** is auto-hedged through the EXO→D1 target pipeline; **vega, gamma, rho** are monitored with Tier-1 firm-level per-underlying limits (`delta-one/d1.toml`) and alerting on `d1.alerts.limits` — never auto-traded; **theta** is monitored as expected carry (ccy/day, the P&L-explain mirror of gamma); **dividend sensitivity** is monitored. Phase 4 (MANDATORY, ADR-009): Tier-2 full-optimizer hedge *proposals* for vega/gamma (human-approved via the UI command path, executed by Delta One) and directed rho *transfer* to RATES-IR with the external futures hedge generated through the ordinary pipeline. Tier-3 fully automated vol hedging remains explicitly rejected.

## Failure and reconciliation model

- At-least-once everywhere off the hot path; dedupe on `msg_id` (UUIDv7).
- Order state machine keyed by `ClOrdID`; FIX session recovery per QuickFIX (sequence/resend) tested against `sim/`.
- EXO↔D1 position reconciliation: EXO halts target publication per book on divergence and alerts (`exo.alerts.recon`), see `exo/CLAUDE.md` rule 5.
- UI treats disconnect as stale: full-board grey-out + snapshot refresh on reconnect.
- Kill switch (Phase 3): NATS command → Delta One cancels working orders, rejects new targets, stays subscribed (observability survives the halt).

**Ordered shutdown contract (P1.M4 Slice 3):** threading pipelines that push data through async gateways require coordinated drains. The `d1-posttrade` Kafka producer thread is the model: it consumes `PostTradeEvent` from a ring pushed by the core thread. Contract: (1) signal the core's `shutdown` flag, (2) join the core thread — it drains all pending events before exiting — (3) signal the producer's `posttrade_shutdown` flag, (4) join the producer thread. This ordering prevents the producer from exiting while the core is still draining, which would silently drop in-flight events. Any future gateway thread (e.g., Rates-IR FIX adapter for Phase 4 rho transfer) follows the same pattern.

**Post-trade topic provisioning (P1.M4 Slice 2):** the `posttrade.trades`, `posttrade.crosses`, `posttrade.allocations`, and `posttrade.orders.audit` topics must exist before the producer starts. Compose sets `KAFKA_AUTO_CREATE_TOPICS_ENABLE: "false"` (safe default); the topics are provisioned out-of-band via `scripts/demo.sh` at startup. The producer hard-errors if any topic is missing — this is a deployment prerequisite and a guard against silently losing post-trade data.

## Demo storyline (implemented as a `sim/` scenario)

1. Underlying gaps down through an autocall barrier.
2. EXO revalues; structured-product book delta jumps; publishes new targets.
3. Delta One nets EXO demand against tracker-book demand → explicit internal cross (booked, visible in UI blotter) + small external residual.
4. Residual executes over FIX against the sim acceptor; fills allocate pro-rata; post-trade events land in Kafka.
5. UI shows the full lineage: target → cross → order → fills → allocations, plus valuation metadata (model, seed, paths, git sha) for the EXO number.

Extended storyline (`advanced-hedging` scenario, Phase 4):

6. Vol regime shifts in the sim; the book's vega breaches its Tier-1 limit → `RiskLimitAlert`; EXO's optimizer emits a `HedgeProposal` (option basket, pre/post exposure, modeled cost).
7. Trader approves on the UI (`d1.cmd.proposal`); Delta One validates and executes the legs over FIX; post-exposure lands back inside the limit; everything books through the normal post-trade plane.
8. Accumulated USD rho crosses its threshold → `InternalTransferRequest` books a directed cross EXO-SP → RATES-IR; RATES-IR's flat-target logic emits the external ZN/SR3 hedge through the same pipeline; UI shows the full transfer-to-hedge lineage.
