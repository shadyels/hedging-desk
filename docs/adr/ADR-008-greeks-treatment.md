# ADR-008: Greeks treatment — hedge / limit / monitor ladder

**Status:** Accepted
**Date:** 2026-07-06
**Deciders:** desk lead (options chosen explicitly; see consequences for sources)

## Context

The structured-product book carries delta, gamma, vega, rho, theta and
dividend sensitivity. The system must decide, per Greek, which of three
treatments applies: automated hedging, limit-enforced monitoring, or
monitoring only. Relevant externals: vega hedging of exotics is standard
dealer practice executed as a dynamic options strategy with material
transaction costs (Bel Hadj Ayed & Loeper, 2023,
https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4550643); the optimal
vega hedge is a vanilla-option basket weighted by vega and liquidity
(Guéant & Pu, 2020, https://arxiv.org/pdf/2005.10064); rate risk is hedged
with linear instruments — swaps and bond futures (PIMCO,
https://www.pimco.com/us/en/resources/education/understanding-interest-rate-swaps).

## Decision (per Greek)

| Greek | Treatment (Phases 1–3) | Phase 4 (MANDATORY, per ADR-009) |
|---|---|---|
| Delta | **Auto-hedged**: EXO targets → D1 netting → linear execution | — |
| Gamma | Monitored + **Tier-1 limit/alert**; managed implicitly via rehedge banding | Tier-2 option hedge proposals |
| Vega | Monitored + **Tier-1 limit/alert** | Tier-2 full-optimizer hedge proposals (ADR-009), human-approved, executed by D1 |
| Rho | Monitored + **Tier-1 limit/alert** | **Directed internal transfer to RATES-IR (ADR-009 mechanics) AND the external futures hedge generated for RATES-IR through the ordinary pipeline** |
| Theta | **Monitored as expected carry** (P&L-explain line, ccy/day) | — (no hedge exists; deterministic decay is gamma's accounting mirror) |
| Div sens | Monitored | revisit with desk |

Tier-1 mechanics: firm-level per-underlying warn/hard limits in
`delta-one/d1.toml` (config, never code); Delta One aggregates its own delta
with EXO-published Greeks, checks limits, and publishes `RiskLimitAlert` on
`d1.alerts.limits`. A hard breach never auto-trades options; it alerts and is
surfaced unmissably in the UI.

Explicitly rejected: Tier-3 fully automated vol hedging (options RFQ
execution + hedge optimizer; prevalence on real desks unverified — treated as
out of scope regardless).

## Consequences

- Easier: exposure cannot grow silently (the desk-exposure concern that
  triggered this ADR); demo shows a complete risk picture, not delta-only.
- Harder: Delta One takes on Greek aggregation and limit checking (off hot
  path); Phase 4 rho transfer needs rate instruments and DV01 conventions in
  refdata before it can be built.
- Honest caveat recorded: the claim that equity-exotics desks conventionally
  transfer rates/funding risk internally to a rates desk/treasury is common
  practice as described in training data but was not verified against a
  primary source; firms vary. The design does not depend on it — direct
  hedging via the same pipeline remains available if the desk prefers.
