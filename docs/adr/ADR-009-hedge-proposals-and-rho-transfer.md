# ADR-009: Tier-2 hedge proposal engine (full optimizer) and rho transfer with external hedge

**Status:** Accepted
**Date:** 2026-07-06
**Deciders:** desk lead — full optimizer chosen explicitly over a simplified v1;
rho transfer generates the external hedge (not transfer-and-stop). Both are
**mandatory demo scope** (Phase 4), not backlog.

## Context

ADR-008 set the ladder: vega/gamma limited+monitored with proposals as the
next tier; rho transferred internally to RATES-IR. This ADR specifies both
mechanisms concretely. Literature anchor for the optimizer: optimal hedging
of exotics with vanillas is a basket-selection problem over the liquid
vanilla universe, weighting by vega and liquidity/transaction cost
(Guéant & Pu, 2020, https://arxiv.org/pdf/2005.10064); dealer vega hedging is
a dynamic strategy whose transaction costs are first-order (Bel Hadj Ayed &
Loeper, 2023, https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4550643).

## Decision — proposal engine (vega/gamma)

1. **Trigger:** Tier-1 warn/breach on `d1.alerts.limits`, or trader request
   from the UI.
2. **Optimizer (EXO `hedger/` package), our formulation informed by the
   literature above (labeled as our design, not a sourced algorithm):**
   given underlying u with exposure vector g = (vega, gamma) and the listed
   option universe O(u) from refdata with per-instrument Greek vectors A and
   modeled cost c(w):
   `min_w  || g + A·w ||²_Λ  +  λ·c(w)`
   subject to per-instrument liquidity caps and integer contract rounding.
   Λ (risk weights) and λ (cost aversion) live in `exo.toml [hedger]`; both
   are desk-visible parameters, never hardcoded. Solver: small QP/greedy over
   the chain — the universe is listed options only; no OTC in demo scope.
3. **Output:** `HedgeProposal` on `exo.proposals.<book>` — legs with limit
   prices, pre/post exposure, modeled cost, `valid_until_ns`, full
   `ValuationMeta`. Stale proposals are dead: re-generate, never execute.
4. **Approval & execution:** trader approves/rejects via `d1.cmd.proposal`
   (`ProposalDecision`). Delta One validates (proposal known, unexpired,
   passes the same risk checks as any flow) and executes legs over FIX as
   ordinary orders for the proposing book; fills/booking flow through the
   normal exec/post-trade planes. Rejections are audited with reason
   (`posttrade.orders.audit`, origin=MANUAL_UI).
5. EXO never executes; Delta One never invents hedges. The proposal/approval
   boundary is the compliance line.

## Decision — rho transfer with external hedge

1. EXO aggregates book rho per currency/tenor bucket (demo: one USD bucket).
2. On breach/threshold, the rho risk is **transferred** to RATES-IR via the
   ADR-005 internal-cross machinery (explicit booked transfer, cross ref
   price policy applies, full lineage on `posttrade.crosses`).
3. **RATES-IR then hedges externally:** EXO's rates mapping converts the
   book's accumulated rho to rate-future targets using refdata DV01s
   (`rho_hedge_instrument_ids`, `dv01_e9_DEMO_PLACEHOLDER` — demo values,
   desk-calibrated in reality) and publishes ordinary `TargetPosition`s for
   RATES-IR; the existing netting/execution pipeline does the rest. No new
   execution machinery.

## Mechanics note — directed crosses

Netting-cycle crosses arise from opposite demands; a risk *transfer* cannot —
if EXO-SP and RATES-IR both demanded the hedge, netting would either find no
offset (same side) or misallocate the external order. Therefore `d1-netting`
exposes a second entry point: **directed cross by instruction**
(`InternalTransferRequest` on `exo.transfers.<book>`). It shares the entire
booking path with netting-generated crosses (same reference-price policy,
same `InternalCrossNotice`, same Kafka records, same lineage), differing only
in trigger. After the cross, RATES-IR's flat-target logic generates the
external futures hedge through the ordinary pipeline, allocated to RATES-IR.

## Consequences

- Easier: the demo's flagship narrative doubles — breach → proposal →
  approval → options execution → booked; and rho → internal transfer →
  external futures hedge, all on existing rails.
- Harder: real quant scope in EXO (`hedger/` optimizer with tests: the
  proposal must verifiably reduce ||g|| under its own model; property test:
  post_exposure recomputed from legs matches the proposal's claim within MC
  error). D1 gains proposal caching/validation state.
- Honest demo boundary: option chain is minimal and DV01s are placeholders —
  labeled as such in refdata; the demo claims workflow + optimizer
  correctness, not market-calibrated hedge quality.
