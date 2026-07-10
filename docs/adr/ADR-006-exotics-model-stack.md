# ADR-006: Exotics model stack — Monte Carlo under Heston(-LV), full product list phased

**Status:** Accepted
**Date:** 2026-07-05
**Deciders:** desk lead

## Context

Requested products: autocallable, reverse convertible, bonus certificate, barrier reverse convertible, TARF, PTARF. Condition given: use all of them if MC + Heston(-local-vol) convincingly covers them, otherwise restrict to autocallable + barrier.

## Analysis (own analysis, not a cited claim)

- Autocallable, reverse convertible, barrier reverse convertible, bonus certificate are all equity barrier/coupon payoffs on a simulated path — one MC engine + one payoff abstraction covers them; the differences are payoff code only.
- TARF/PTARF are FX path-dependent products (periodic fixings, accumulated gain, target knockout). The same Heston-type stochastic-vol dynamics apply to an FX spot with domestic/foreign rate drift, so the *engine* covers them. What MC+Heston does **not** give convincingly for a real desk is FX smile calibration quality; for a demo with a documented (not market-calibrated) parameter set, pricing and delta hedging behavior are demonstrable and honest as long as we label the calibration as illustrative.

Conclusion: the engine covers all six; calibration realism is the caveat, not the model structure. Therefore: keep all six, phased (equity products first, FX products in M3), and state the calibration caveat explicitly in the demo.

## Decision

- Dynamics: Heston (equity, and FX with two rates in M3); Heston-local-vol leverage surface as M4 stretch. Discretization: QE scheme (Andersen) or full truncation Euler — choose in M1 with a convergence test, document.
- Pricing: vectorized NumPy MC, antithetic variates baseline; numba for path-state kernels (barrier monitoring, TARF accumulation) when profiling demands.
- Greeks: bump-and-revalue with common random numbers, standard errors reported; pathwise deltas where straightforward.
- Validation gates per product (blocking): degenerate-to-BS closed-form checks for barriers; Heston vanilla via characteristic function; MC within 3 standard errors.
- PDE solver: M4, single-asset barrier cross-check only. Not demo-critical.

## Consequences

- Easier: one engine, six products, credible breadth for the showcase.
- Harder: the payoff abstraction must be designed for path-dependent state from day one (TARF needs it) even though M1 products don't — see `exo/CLAUDE.md` M2 rule ("if a new payoff touches models/, the abstraction is wrong").
- Explicit non-goal: market-quality calibration. Do not let the demo imply it.

## Amendment (2026-07-06)

Scope correction by desk lead: "minimal" always referred to the development instrument universe, never to project scope. Accordingly the items above marked "M4 stretch" / "not demo-critical" — Heston-local-vol leverage surface and the PDE cross-check pricer — are **mandatory**, scheduled as P4.M4 together with the calibration framework against sim-generated synthetic vanilla surfaces (live-market calibration remains Phase 5, externally gated). See docs/ROADMAP.md and ADR-009.

## Amendment 2 (2026-07-06) — model sufficiency review

Question reviewed: is Heston MC enough for Phase 2? Decision: yes as the validated foundation, no as the end state — which P4.M4 (Heston-LV + PDE + synthetic-surface calibration) already provides. Supporting literature: autocallable value depends on forward-skew and vol-of-vol risk and LSV is better suited than LV (Deelstra & Hussain, 2022, https://www.aimsciences.org/article/doi/10.3934/fmf.2022008?viewType=HTML); LV forward skews are too flat (Haugh, Columbia lecture notes, https://www.columbia.edu/~mh2078/ContinuousFE/LocalStochasticJumps.pdf); SLV described as de facto standard for FX options (Cozma & Reisinger, 2017, https://arxiv.org/pdf/1706.07375). Additions made: QMC + variance reduction promoted into P2.M1; multi-asset worst-of products added as P4.M5; rough volatility parked in Phase 5 (not established desk practice — the project's "market standard or on the route to it" rule). Known limitation recorded: even SLV has documented flaws for autocallables (Risk.net, 2019, https://www.risk.net/topics/stochastic-local-volatility-slv) — the validation-gate culture in exo/CLAUDE.md is the standing mitigation.
