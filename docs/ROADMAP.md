# Roadmap

Scope principle (corrected 2026-07-06): the **instrument universe** starts minimal during development and is enriched at scale in P4.M5 — but **project scope is extensive**. Phases 1–4 are all mandatory and built to the same depth. Each milestone ends demoable.

## Phase 1 — Delta One (Rust) — mandatory

- **P1.M1 — Skeleton & contracts:** compose stack up; protocol codegen; feed ingest from `sim/` replay; position keeper; HDR-histogram bench harness (the latency measurement exists before the features do).
  - Split into two independent slices (ADR-004: Protobuf never enters `d1-core`, so the runtime needs no codegen). **Slice 1 — skeleton runtime** (`d1-core` ids/feed/market-data/keeper modules, `sim` replay mode, criterion+HDR bench): done on `feat/d1-skeleton`. **Slice 2 — contracts** (`gen-proto.sh`, `schema-check.sh`, codegen wired into all three builds; local plugin binaries, ADR-012): done on `feat/d1-skeleton`.
- **P1.M2 — Order path:** order state machine; FIX 4.4 session vs sim acceptor (logon/sequence/resend correct); NewOrderSingle → ExecutionReport round trip; exec reports out on NATS.
  - Split into three dependency-ordered slices. **Slice 1 — order state machine** (`d1-core::order`: `OrderStore`/`Order`/`ExecEvent`/`Fill`, idempotent `apply_exec` via `ExecId` dedupe, unit+proptest coverage, HDR bench): done on `feat/d1-order-state-machine`. **Slice 2 — FIX round trip + threading skeleton** (`d1-gateway-fix` + `quickfix`, `sim` FIX acceptor, `rtrb` SPSC rings core↔gateway per ADR-013, new `d1` binary crate hosting the core thread + FIX gateway, startup synthetic order as the M3 netting-driven-emit stand-in): done on `feat/d1-fix-round-trip`. **Slice 3 — NATS plane + demo** (`d1-gateway-nats` consumes `TargetPosition`, publishes `ExecutionReport`, feed-ingest ring + producer thread, `just demo` wiring): not started.
- **P1.M3 — Netting & crosses:** multi-book netting per ADR-005; explicit internal-cross booking (netting-generated AND directed-by-instruction, see ADR-009 mechanics note); pro-rata allocations; property tests (Σ book positions == firm position under arbitrary interleavings).
- **P1.M4 — Post-trade:** Kafka/Avro producers for trades, crosses, allocations, order audit; deterministic golden-file e2e test.
- **P1.M5 — Tracker analytics (ADR-010):** per-book cash in the position keeper; sim dividend events; ex-post TE, tracking difference and cash drag in `d1-analytics`; `d1.tracker.<book>` + Kafka daily record.
- **Exit:** `just demo` runs the tracker-flow scenario end to end (incl. tracker analytics); bench report p50/p99 within budget on release build.

## Phase 2 — EXO (Python) — mandatory

- **P2.M1 — Engine + first products:** Heston MC engine (scheme chosen via convergence test) with QMC (Sobol + Brownian bridge) and variance reduction (antithetics baseline, control variates where a closed form exists); barrier option + autocallable; validation gates green (BS-degenerate closed forms, Heston vanilla characteristic function).
- **P2.M2 — Equity product set:** reverse convertible, barrier reverse convertible, bonus certificate (payoff layer only).
- **P2.M3 — FX products:** TARF, PTARF under Heston-FX (two rates); illustrative parameter set, calibration caveat documented until P4.M4.
- **P2.M4 — The loop:** portfolio deltas per book, rehedge banding, target publication, fill consumption, reconciliation halt behavior; Tier-1 Greek limits + `d1.alerts.limits` alerting (ADR-008). Full tracker-flow demo storyline (ARCHITECTURE.md) runs.

## Phase 3 — UI (TypeScript) — mandatory

- **P3.M1 — Monitoring:** desk risk board with the full monitored Greek set (delta/gamma/vega/rho/theta/div-sens + limit states) and tracker scorecard (TE / tracking difference / cash drag, ADR-010), blotter with crosses as first-class rows, system health incl. valuation metadata.
- **P3.M2 — Actions:** manual ticket + kill switch + hedge-proposal approve/reject over `d1.cmd.*` request/reply; rejection paths rendered properly.

## Phase 4 — Advanced hedging & enrichment — MANDATORY, same depth as 1–3

- **P4.M1 — Rates foundation:** rate instruments + DV01/tenor conventions in refdata (replacing DEMO placeholders with a documented convention set); EXO rates mapping (rho per ccy bucket → futures-equivalent quantities).
- **P4.M2 — Rho transfer:** directed internal cross EXO-SP → RATES-IR (`exo.transfers.*`, ADR-009); RATES-IR external futures hedge generated through the ordinary target pipeline; golden-file scenario.
- **P4.M3 — Hedge proposal engine (full optimizer, ADR-009):** vega/liquidity QP over the listed option chain; `HedgeProposal` bus flow; D1 proposal cache, validation, execution; UI approve/reject; property test — recomputed post-exposure from proposal legs matches the claim within MC error.
- **P4.M4 — Model depth:** Heston-local-vol leverage surface; PDE pricer as MC cross-check for single-asset barriers; calibration framework calibrating Heston(-LV) to **sim-generated synthetic vanilla surfaces** (framework is real and demoable; swapping in live market quotes is a Phase 5 data change, not a code change).
- **P4.M5 — Multi-asset:** worst-of autocallables/BRCs on 2–3 correlated underlyings (correlated Heston, LSV extension where calibrated); payoff layer reuses the P2 abstraction; correlation is a documented demo parameter until Phase 5 market calibration.
- **P4.M6 — Universe enrichment at scale:** the "go big" step — broad multi-asset instrument sets, full option chains per underlying, additional FX pairs and bonds; `advanced-hedging` extended demo scenario runs end to end (breach → proposal → approval → options execution → booked; rho → transfer → external futures hedge).

## Phase 5 — Optional / externally gated

- Proprietary venue FIX dialects (EMSX, TSOX, FXAll — need specs and entitlements); calibration against live market data feeds;
- JetStream evaluation for replayable target history; 
- **AI assistance (ADR-011)**: an LLM decision-support layer (risk Q&A, morning briefing, proposal narration, scenario authoring — read-only, non-executing) and an ML research track (deep hedging for the rehedge policy, ex-ante TE, NN-accelerated calibration — shadow mode first); 
- rough-volatility models (rBergomi) — research-strong on short-dated skew but not established desk practice for exotics pricing, so optional by the project's own tech rule.
