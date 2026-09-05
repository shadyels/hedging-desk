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

## Amendment 4 (2026-09-05) — P2.M1 scoping, and the reserved home for the discretization decision

P2.M1 readiness was assessed on 2026-09-05 (see `docs/ROADMAP.md` P2.M1). The milestone is
unblocked, but four questions its own text does not settle were answered by desk lead. Three are
decided here; the fourth (§4) is **reserved and pending**, because it is an empirical question and
the experiment has not been run.

### 1. The illustrative model parameter set lives in `exo.toml`, not in `protocol/` refdata

`S0, r, q` and the Heston parameters `(v0, κ, θ, ξ, ρ)` per underlying go in a new
`exo.toml [models.*]` section. They are **not** added to `protocol/refdata/universe.json`.

Rationale, and the reason it needs an ADR at all: root `CLAUDE.md` forbids inventing market
conventions and requires them "defined once in `protocol/` reference data", so a reader could
reasonably conclude a parameter set belongs there. It does not, for two reasons. EXO is its only
consumer — Delta One and the UI never read it, whereas `cross_px_policy_default`, `tick_e9` and
`dv01` are all read by components other than the one that writes them. And this ADR's standing
non-goal ("market-quality calibration remains out of scope; do not let the demo imply it") is
actively undermined by seating uncalibrated numbers in the shared contract surface, where they
acquire the same apparent authority as the real instrument conventions beside them. The precedent
is P1.M5's `d1.toml [tracker]` (a component's own input → component config), not P1.M3's
`cross_px_policy` (a market convention consumed firm-wide → refdata).

Consequence: `protocol/refdata/universe.json` gains no field, and the parameter set carries its own
"illustrative, not calibrated" note at the point of definition.

### 2. P2.M1 is pricing plus standard error; Greeks land in P2.M4

M1 delivers prices with mandatory MC standard errors and no Greeks. ADR-008's monitored set
(delta, gamma, vega, rho, theta, dividend sensitivity) is consumed by target publication and
`ValuationSnapshot`, both of which are P2.M4 deliverables; computing them in M1 would front-load
work with no consumer.

**This defers the estimator, not its plumbing.** `exo/CLAUDE.md` rule 6 requires bump-and-revalue
with **common random numbers** — the same seed per bump pair. An M1 engine that constructs its RNG
privately inside path generation cannot later hand a bumped revaluation the identical normals, and
M4 would have to re-cut that seam under a milestone that is not about numerics. M1 must therefore
expose seed/stream construction as an explicit input even though nothing in M1 bumps anything.

### 3. Product term sheets are test fixtures in M1; no persisted product book until P2.M4

Barrier-option and autocallable instances (strike, barrier, coupon, observation schedule, notional)
are constructed in `pytest` fixtures. No `products.json`, no `[products]` config section, no store.
The persisted EXO book arrives with P2.M4's portfolio loop, which is the first thing that needs one
— and with `sim/scenarios/tracker-flow.yaml`'s `action: exo_book_event`, which P2.M4 already owns.

Secondary benefit worth recording, because it is the reason to prefer fixtures beyond scope
control: a term-sheet file permits loader-side defaults and fixups, which hide an inadequate payoff
definition. A fixture forces every field explicit at the construction site, which is the pressure
that exposes a bad abstraction *before* M2 stacks six more families on it.

### 4. Discretization scheme: QE (Andersen) vs full-truncation Euler — **PENDING**

**Status: reserved, not decided.** The Decision section above defers this to "M1 with a convergence
test, document"; this subsection is that document's home, and P2.M1 slice 1 fills it. Recording it
as pending rather than guessing is deliberate — the choice is empirical and no convergence study has
been run.

What must appear here when slice 1 completes: the scheme chosen; the convergence evidence (bias and
variance vs step count, against the characteristic-function vanilla reference); and the handling of
the Feller condition, since the two schemes differ precisely in how they behave when it is violated,
which is the regime the illustrative parameter set may well sit in.

## Consequences (Amendment 4)

- **P2.M1 requires no new dependency.** `exo/pyproject.toml` already pins `numpy`, `scipy` (for the
  characteristic-function gate), `hypothesis` and the lint/type toolchain. `numba` remains deferred
  on this ADR's existing terms ("when profiling demands"), and adding it in M1 would need its own
  justification.
- **Amendment 3's LSM sanction constrains the M1 engine, not just M2's payoffs.** Longstaff–Schwartz
  regresses backwards over the simulated state at every exercise date, so it needs the retained path
  matrix — and, per Amendment 3 §1, retained in `(S, v)` rather than `S` alone. A forward-streaming
  engine that discards paths as it accumulates the discounted payoff would be the natural M1 design
  under barrier + autocallable alone, and would make American warrants unimplementable without
  re-cutting `models/`. Amendment 3 licenses *adding* LSM to `models/`; it does not license
  rebuilding the engine underneath it. M1's choice of what a "path" is decides this.
- **The M1 validation gate and the M2/M3 control variate are the same analytics.** No closed form
  exists for a barrier under Heston, so the natural control variate is the Heston vanilla — priced
  by the characteristic function M1 must implement anyway as its blocking gate. The gate machinery
  should be built as a reusable pricer, not buried in a test module.
- **`ValuationSnapshot.ProductLine.pv_std_err_e9` is a required field** (`protocol/proto/live.proto`),
  which makes this ADR's "MC within 3 standard errors" culture structurally enforced rather than
  merely encouraged: a pricer that cannot report its own standard error cannot fill the message.
  Pricers return an estimate and its standard error together.
- **QMC and antithetics are not independently composable, and the milestone text reads as if they
  are.** Antithetic pairing of a Sobol sequence destroys the low-discrepancy property that motivated
  Sobol, while the Brownian bridge is what makes Sobol effective at all by concentrating variance in
  the leading dimensions. Amendment 2 promoted "QMC + variance reduction" into P2.M1 as one clause;
  it is two designs requiring reconciliation, and the reconciliation must be stated with the scheme
  decision in §4 rather than left to whichever code lands last.
- **No change to the instrument universe, the wire format, or any other component.** This amendment
  moves nothing onto the bus and adds no `protocol/` field; it records where EXO-private inputs live
  and what M1 does not build.
