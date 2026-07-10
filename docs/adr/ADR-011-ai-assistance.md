# ADR-011: AI assistance in the hedging process (Phase 5)

**Status:** Proposed (Phase 5 exploration — acceptance requires desk + model-risk review) **Date:** 2026-07-06

## Context

Desk lead requested Phase 5 work exploring how AI can assist hedging. Two distinct tracks exist with different risk profiles; both inherit the system's standing principle (ADR-008/009): assistive intelligence proposes and explains, humans approve, only Delta One executes.

## Track A — LLM/agent decision support (assistive, non-executing)

Integration: a read-only consumer of NATS snapshots and Kafka history; provider-agnostic LLM API; NEVER connected to `d1.cmd.*` or any execution path. Candidate capabilities, in rough order of value/effort:
1. Natural-language risk queries ("why did EXO-SP vega breach at 14:02?") answered from `RiskSnapshot`/`RiskLimitAlert`/`ValuationSnapshot` history.
2. Morning desk briefing: overnight fills, crosses, limit events, TE/cash-drag moves (ADR-010), open proposals — generated from Kafka, cited to records.
3. `HedgeProposal` narration: translate optimizer output (legs, pre/post exposure, cost) into trader-readable rationale — explanation, not advice.
4. Recon/anomaly narration for `exo.alerts.recon` and audit-trail diffs.
5. Scenario authoring: natural language → `sim/` YAML scenarios. Guardrails: read-only credentials enforced at NATS/Kafka ACL level; every generated statement must cite the underlying record ids; hallucination policy: no record, no claim.

## Track B — ML/quant research (shadow mode first)

1. **Deep hedging:** framework for hedging derivative portfolios under market frictions (transaction costs, market impact, liquidity constraints, risk limits) using deep reinforcement learning — Buehler, Gonon, Teichmann, Wood, "Deep Hedging", 2018/2019, https://arxiv.org/abs/1802.03042. Candidate use: learn the rehedge policy (replacing/augmenting static banding in `exo/src/exo/portfolio/`) trained against `sim/` scenarios; recent work reduces training-data requirements (Brugière & Turinici, 2025, https://arxiv.org/abs/2505.22836) and applies RL under realistic costs and position limits (arXiv, 2025, https://arxiv.org/abs/2512.12420).
2. **Ex-ante tracking error** (ADR-010 deferral): predictive TE model per tracker book.
3. **NN-accelerated calibration** for the P4.M4 calibration framework (candidate direction, our analysis; no source vetted yet). Guardrails: SHADOW MODE mandatory first — model recommendations are logged next to production decisions and compared over a defined period before any live routing; model-risk documentation (data lineage, retraining policy, kill criteria) is a precondition of acceptance; a learned policy publishes `TargetPosition`s through the exact same pipeline and limits as any other source, never a private execution path.

## Consequences

- Track A is buildable on existing rails (read-only bus consumer) with low model risk; Track B is research with a defined promotion path (shadow → reviewed → live) and explicit rejection of autonomous execution.
