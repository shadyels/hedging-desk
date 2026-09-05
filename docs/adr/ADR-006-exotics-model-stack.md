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

## Amendment 3 (2026-09-05) — warrants and mini futures added to Phase 2

Scope addition by desk lead: the product set goes from **six families to eight**. **Warrants**
(issued call and put certificates, European *and* American exercise) and **mini futures**
(open-ended leverage certificates, long and short, full mechanics) are scheduled into **P2.M2**.
M3/M4 milestone numbering is unchanged.

**Engine sufficiency** — the same test this ADR applied to the original six:

- A **warrant** is a vanilla call/put in a certificate wrapper (strike, expiry, ratio/parity,
  exercise style). Model cost is near zero, and the European leg validates *exactly* against the
  Heston characteristic function already standing as a P2.M1 gate. No new dynamics.
- A **mini future** is a continuously-monitored knock-out plus a daily financing accrual, settling
  at residual value on stop-out. Both are path-dependent payoff state of precisely the kind the
  payoff abstraction was already required to carry for TARF (see Consequences above). No new
  dynamics.

**The sanctioned model-layer addition (the one real consequence).** American exercise cannot be
priced by vectorized forward Monte Carlo; it needs a backward regression pass —
**Longstaff–Schwartz (LSM)** — whose engine belongs in `models/`, not in a payoff. Two points that
are the ones that get got wrong:

1. Under Heston the continuation value depends on the variance level, so LSM **must regress the
   continuation value on `(S, v)`, not on `S` alone**. An S-only basis is biased, and that bias is
   *invisible* against a BS-degenerate test, where `v` is constant — the degenerate gate cannot
   catch it, which is why it is recorded here instead.
2. LSM is a **low-biased estimator**: it yields a *lower bound* on the true American value. Publish
   the bias direction alongside the number; never present an LSM price as unbiased.
   Andersen–Broadie dual upper bounds are explicitly **out of scope**.

Accordingly the M2 rule in `exo/CLAUDE.md` is **narrowed, not deleted**: "if a new payoff touches
`models/`, the abstraction is wrong" governs **payoff** additions. **Exercise style** is a numerics
property rather than a payoff, and its engine legitimately lives in `models/`. Adding LSM does not
license any other `models/` change in M2.

## Consequences (Amendment 3)

- **P4.M4's PDE pricer gains a second cross-check duty:** American exercise, alongside the
  single-asset barrier cross-check it already owned — now that P2 prices American warrants by LSM,
  the PDE is the independent check on that number.
- **`product_type` grows by comment, not by field number.**
  `ValuationSnapshot.ProductLine.product_type` (`protocol/proto/live.proto`) takes four new values:
  `"warrant_call" | "warrant_put" | "mini_long" | "mini_short"`. The vocabulary is a comment on a
  `string` field, so this is **not** a field-number change and **not** a schema-compat event. Flat
  values (rather than one `"warrant"` plus a new side/exercise field) match the existing coarse
  style and let the UI blotter render the distinction with no proto change. **Exercise style stays
  inside the EXO product definition and is deliberately not on the wire** — it does not change how
  the risk row displays.
- **The instrument universe and `InstrumentClass` are unchanged.** Both families are *desk-issued*
  products carried as `ProductLine` rows in book `3 EXO-SP`, not `instrument_id`s Delta One
  executes; their hedges are the existing underlyings. So `protocol/refdata/universe.json` gains no
  row, `common.proto`'s `InstrumentClass` gains no value, and the **P4.M6 "enrich at scale" gate is
  unaffected**. In particular a warrant is a `ProductLine`; the listed options already in the
  universe (ids 3001–3005) remain hedge `instrument_id`s. They are not the same object.
- **The explicit non-goal stands:** market-quality calibration remains out of scope, exactly as the
  Consequences section above states. Adding two families does not soften it.
