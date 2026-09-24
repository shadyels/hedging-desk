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

## Amendment 4 (2026-09-05) — P2.M1 scoping, and the discretization scheme decision (resolved in §4 below)

P2.M1 readiness was assessed on 2026-09-05 (see `docs/ROADMAP.md` P2.M1). The milestone is
unblocked, but four questions its own text does not settle were answered by desk lead. Three are
decided here; the fourth (§4) is **resolved in the subsection below**, with empirical evidence from the convergence study.

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

### 4. Discretization scheme: QE (Andersen) vs full-truncation Euler

**Scheme chosen: QE (Quadratic-Exponential), Andersen 2008 formulation with branch-dependent martingale correction and central discretization (gamma1 = gamma2 = 1/2).** The convergence study in `docs/studies/p2m1-scheme-convergence.md` fills this subsection with evidence, resolving the reserved choice.

**Convergence evidence.** The study swept **two synthetic parameter sets**, held as local constants in `exo/src/exo/studies/scheme_convergence.py` and deliberately *not* loaded from `exo.toml`, so the study's grid cannot drift if the config is later tweaked for an unrelated reason. Both sit at `s0=100, r=0.02, q=0.01`, a common spot chosen so the `{90, 100, 110}` strike grid lands ITM/ATM/OTM for a call on each. Their Heston texture mirrors two of the configured underlyings: a Feller-satisfying set (`v0=0.05, k=2.0, theta=0.05, xi=0.35, rho=-0.55`, ratio 1.633 — MSFT's texture) and a Feller-violating set (`v0=0.04, k=1.5, theta=0.04, xi=0.6, rho=-0.7`, ratio 0.333 — AAPL's texture). All eight parameters of each are recorded in the run manifest `exo/run-manifests/2026-09-07T21-10-10Z-scheme-convergence.toml`. **`exo.toml`'s NESN (1.378) and SPX (0.231) sets were not simulated**, and SPX's 0.231 is materially more Feller-violating than the 0.333 tested here, so this decision is validated at ratio 0.333 rather than across the full configured range — see the carried risk below. `exo.toml` retains two sets of each kind with a test asserting both regimes remain present, but that test constrains the config, not this study's grid. Bias and variance were measured against the Heston vanilla characteristic-function reference (little-trap formulation). The first-passing criterion is |bias| < 3·shared_se across all 12 rows at that step count (2 parameter sets × 3 strikes × 2 expiries), where shared_se is the *minimum* standard error among schemes at each cell, preventing passage by noise alone. QE first satisfies this at n_steps=12 (4,615,380 paths, 19.066 s wall time); full-truncation Euler first satisfies at n_steps=104 (3,238,092 paths, 50.581 s). The cost-to-equivalent-accuracy ratio is 2.65x in QE's favour (wall_time_euler / wall_time_qe). This comparison is conservative: QE ran 42% more paths than Euler and carried the inflated wall time that implies, yet still won on elapsed time.

Euler's convergence point is sampling-sensitive — n_steps=252 under an earlier batch plan, n_steps=104 under the corrected one — while QE's first-passing step count was n_steps=12 under both. At n_steps=4, a coarse grid point, QE passes 9 of 12 rows. Euler fails every row at n_steps=4 — both regimes, 0 of 12 — and 11 of 12 at n_steps=12. The scheme with the wider margin is also the one whose convergence point is stable across operational batch-plan changes.

**Feller condition handling.** The two schemes differ precisely in how they behave when the Feller condition is violated, which is the regime the illustrative parameter set may well sit in. QE's variance process maintains non-negativity by construction: the quadratic-branch update computes `v' = a(b+Z)²`, a square and therefore ≥ 0, and the exponential-branch update draws from a distribution supported on [0, ∞). The martingale correction, applied to the log-spot drift K0* to keep the discounted spot an exact martingale per step, becomes inadmissible in certain cells and falls back to an uncorrected K0 — a separate mechanism. Full-truncation Euler permits the variance state to go negative and truncates only its *use* in drift and diffusion calculations. The study was designed to expose this difference: in the Feller-violating regime where Euler's bias explodes as n_steps decreases, QE's bias is controlled from n_steps=12 onward and is already within tolerance on 9 of 12 rows at n_steps=4, where Euler is within tolerance on none.

**Variance reduction (antithetics baseline).** Slice 1 implements antithetic variates and measures genuine variance reduction in both schemes. At a representative cell (Feller-violating regime, ATM, T=0.5 years, n_steps=104, n_paths=190,476), the ratio se_plain / se_antithetic is 1.3937 for QE and 1.3715 for Euler. Antithetics are applied to the driving random draws — the RNG source mirrors them (`Z -> -Z` for normals, `u -> 1-u` for uniforms) *before* either scheme consumes them, and the pairing structure travels with the paths so the estimator collapses to pair means rather than treating the mirrored draws as independent. Both schemes carry them, though the mirroring rule differs by sampler: QE draws uniforms for the variance step and normals for the spot step, full-truncation Euler draws normals for both. The Sobol quasi-random sequence and Brownian-bridge reconciliation with antithetics is explicitly deferred to slice 3, which owns QMC. Slice 1 baseline is pseudo-random normals with antithetic pairing.

**Carried risk — the tested violating regime is not the harshest configured one.** The scheme is validated at Feller ratio 0.333. `exo.toml [models.SPX]` sits at 0.231, which is further into the regime where the two schemes diverge, and was not swept. P2.M2's warrant gate is the first thing to price SPX and is therefore the trigger to either extend the sweep to 0.231 or record that QE holds there.

**Illustrative and uncalibrated parameters.** The Heston parameter sets swept in this study are demo values, not calibrated to market data (ADR-006 Amendment 4 §1 non-goal, restated at the point of use). The scheme choice is validated under THESE illustrative parameters; it is not a universal claim and must not be read as one.

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

## Amendment 5 (2026-09-15) — P2.M1 Slice 2: payoff abstraction + barrier + autocallable

P2.M1 Slice 2 ships the payoff abstraction, barrier option and autocallable term sheets,
and the Reiner–Rubinstein reference pricer. This amendment records three key design decisions
made in this slice and ratifies the undiscounted, dated cashflow ledger as the canonical
representation for all path-dependent structured products.

### 1. The payoff abstraction is an undiscounted, dated cashflow ledger

The `CashflowLedger` dataclass carries two fields: `t`, an ascending array of payment times
in years, shape `(n_obs,)`; and `amounts`, a 2D array shape `(n_paths, n_obs)` of undiscounted
cashflows in product currency. Row order matches the `PathBundle` path order exactly — it
is positional, so methods like the antithetic-pair standard-error estimator can rely on it.

**Reasoning.** Early redemption (autocallable), coupon strips, P2.M3's TARF accumulation,
and P2.M2's mini-future stop-out settlement all become "a cashflow on a different date"
rather than four bespoke mechanisms. Discounting is centralized in `pricer.discount` and
tested once. Keeping `r` out of the payoff means a payoff's shape does not change when P2.M4
bumps `r` for rho — a payoff that discounted internally would change shape under a rate bump,
requiring its own bump-awareness to stay consistent.

**Known ceiling: path-independent payment dates (marker `P2-6` in `products/base.py`).** The
`t` array is shared across all paths, so no payoff on this ledger can emit a cashflow whose
*date* varies by path. Two products hit this ceiling — P2.M2's mini future (each path stops
out on its own date) and P2.M2's LSM American exercise (each path exercises on its own date) —
but it is an escape hatch, not a broken abstraction. The fix survives without re-cutting: widen
`amounts` to `(n_paths, n_steps+1)` against the full simulation grid and emit one non-zero
column per path. This changes how one payoff *populates* the ledger, not the ledger itself,
`CashflowLedger.t`, its shape checks, `discount()`, `price_from_bundle` or the estimator.

**Two sharpenings of the ceiling.** The architect added clarity on what `P2-6` actually blocks:

- **The mini future may never need what `P2-6` names.** Rao-Blackwellization (replacing a
  0/1 survival indicator with its conditional expectation) gives the bridge survival
  *probability* at each step, not a crossing time. This enables an expected-residual-value
  formulation weighted by incremental knock-out probability at each step — wide grid,
  path-independent dates, no escape hatch needed.
- **For LSM the gap is a protocol-surface problem, not a dating problem.** Longstaff–Schwartz
  cannot be expressed as a forward `cashflows(bundle) -> ledger` at all — it needs an
  intrinsic-value accessor at every exercise date and discounting *inside* the backward
  induction. ADR-006 Amendment 3 already pre-sanctions this: exercise style is a numerics
  property, engine in `models/`, so the escape is not a widened `amounts` but a new method
  on the payoff-side surface. This prevents a future reader from concluding the ceiling is
  absolute and re-cutting the abstraction unnecessarily.

**Caveat (architect review, P2.M2 mini future).** It also does not survive P2.M2's mini future,
for a different reason. ADR-006 Amendment 3 defines a mini future as a continuously-monitored
knock-out *plus a daily financing accrual*, and financing accrual is rate-driven — the payoff will
need a rate (risk-free plus issuer spread) as an input, and under a P2.M4 rho bump that leg
**should** move. That is genuine rho, not the artifact this rationale warns against (a payoff that
discounts internally and so needs its own bump-awareness just to stay consistent with an external
rate it never sees again). The M2 implementer has two shapes available: a rate field on the mini-future
term sheet (which P2.M4's bump loop must then know to bump), or an `r` argument threaded through
`cashflows` (which touches every payoff's signature). Both are `products/`-local changes — neither
requires touching `models/` — so neither violates the M2 rule. Spelling this out so an M2 reader
who hits the financing leg does not conclude the abstraction is broken and re-cut it unnecessarily.

### 2. Barrier monitoring is declared by the term sheet: `CONTINUOUS_BRIDGE` or `DISCRETE`

Two monitoring modes are built in this slice: `Monitoring.CONTINUOUS_BRIDGE` uses a
Brownian-bridge per-step survival probability; `Monitoring.DISCRETE` checks a declared
observation schedule with a hard indicator. Each is exercised by a real product built in
the same pass — barrier option and P2.M3's TARF will use them.

**Why the bridge was necessary to meet the gate.** The roadmap's validation gate is "barrier
vs the BS closed form with the model degenerated, within 3 standard errors". The Reiner–Rubinstein
closed form prices **continuous** monitoring; an MC that tests the barrier once per step prices
**discrete** monitoring. At representative parameters (`S0=100, H=90, sigma=0.2, T=1, m=252`),
pricing the same term sheet under plain `DISCRETE` against the continuous-monitoring closed
form misses by **4.01 standard errors** — the gate, written literally, fails on correct code.

The resolution is on the MC side, not the reference side: apply the Brownian-bridge
per-step survival probability (Glasserman, *Monte Carlo Methods in Financial
Engineering*, §6.4), giving `p_cross_i = exp(-2 * ln(S_i/H) * ln(S_{i+1}/H) / (v_i * dt))` with
`w_survive = prod_i (1 - p_cross_i)` and exactly 0 on any path whose endpoint already breached.

**Three reasons this beats correcting the reference.** First, the gate becomes bias-free. Under
BS-degenerate parameters variance is constant, so the bridge is exact and `|mc - ref| < 3*SE`
holds at *any* path count. Under a BGK continuity correction the residual is `o(1/sqrt(m))` and
unbounded relative to shrinking SE, so the path count would have to be silently capped. Evidence
from code review: 12 seeds at 20k paths give bias/SE = **+0.99** (indistinguishable from zero),
and the z-score does not grow with path count — 20k → −1.54, 80k → −1.01, 320k → +0.26.

Second, it reduces variance for free — replacing a 0/1 survival indicator with its conditional
expectation given the discrete skeleton is a Rao-Blackwellization. Third, it makes the payoff
smooth in `S`, which matters for P2.M4's bump-and-revalue delta: a hard indicator barrier is
severely noisy under bumping.

Both modes exist because each has a named future consumer: `CONTINUOUS_BRIDGE` is what P2.M2's
mini future needs for continuous knock-out, `DISCRETE` is what P2.M3's TARF needs for monthly
fixings. Building both now, each exercised by a real product, is the minimal spanning set.

**Two approximations in the bridge.** Survival is a *probability* carrying no crossing time
(marker `P2-1` in `products/base.py`), so no rebate can be dated. And step vol is taken at the
*left point* `sqrt(v_i)`, exact under BS-degeneracy but an approximation under stochastic vol
(marker `P2-2`), triggering P4.M4's PDE cross-check.

### 3. The autocallable is gated by degenerate identities, not digital-strip replication

Two gates exercise the autocallable's correctness directly against exact closed forms:

**G3a — coupon barrier triggers early redemption.** If trigger and coupon barrier are below
every simulated path, every path redeems at observation 1. Then `pv == notional*(1+coupon_rate)*exp(-r*t1)`
exactly, with `se == 0`.

**G3b — autocallable reduces to a short put at the terminal leg.** Set `protection_barrier == initial_level`
and `coupon_barrier` unreachably high. The payoff collapses exactly: `notional*[1{S_T>=B} + (S_T/S0)*1{S_T<B}]`
becomes `notional - (notional/S0)*(S0-S_T)^+`, a short put struck at `S0`. Reference is
`notional*exp(-r*T) - (notional/s0)*heston_vanilla_price(params, strike=s0, expiry=T, is_call=False)`.
The notional leg is discounted because it is a cashflow at `T`, and the ledger
is undiscounted-and-dated by design. This turns a product with no closed form into an exact
comparison against analytics that already exist and are already gated.

Setting `coupon_barrier == autocall_trigger` recovers snowball behaviour — one implementation
covers both shapes.

**Fixture scope: AAPL and MSFT only.** Amendment 4's scheme-convergence study validated the QE
discretization at Feller ratio 0.333 (AAPL's regime) and measured the Feller-violating regime
up to 0.333. `exo.toml`'s SPX sits at 0.231, further into that regime, and was not swept. Rather
than fire the carried risk early, this slice uses only AAPL (0.333, near the tested boundary) and
MSFT (1.633, Feller-satisfying). SPX's 0.231 regime is explicitly left to P2.M2's warrant gate,
which Amendment 4 already names as the trigger for that carried risk.

## Consequences (Amendment 5)

- **No new dependency, no component changes, no wire-format changes.** The payoff abstraction is
  `exo/` only, the two products are fixtures (not persisted), and the reference pricer (`bs_barrier_price`
  in `models/analytic.py`) follows Amendment 4's explicit direction: built as a reusable pricer
  rather than buried in a test module.
- **The `exo/CLAUDE.md` M2 abstraction rule is satisfied at the outset.** The only `models/` change
  is the gate reference pricer, which Amendment 4 explicitly directed be built as a reusable pricer
  — the same reasoning that put `heston_vanilla_price` in `models/`. No payoff touches `models/`.
- **The abstraction's known ceiling is `P2-6` (path-independent payment dates), owned by P2.M2.**
  Both escape hatches — wider grid for mini futures, new payoff protocol method for LSM — are
  `products/`-only changes that do not breach the M2 rule. No other component is touched.

## Amendment 6 (2026-09-24) — P2.M1 Slice 3: QMC, the antithetics reconciliation, and control variates

Slice 3 ships scrambled-Sobol quasi-Monte Carlo with Brownian-bridge dimension ordering, a
Heston-vanilla control variate for barriers, RQMC validation gates, and the variance-reduction
study in `docs/studies/p2m1-qmc-variance-reduction.md`. Amendment 4's Consequences required that
the QMC/antithetics reconciliation be recorded here "rather than left to whichever code lands
last". §1 below is that record; §2–§6 are the decisions that came with it.

### 1. QMC and antithetics do not compose. Sobol REPLACES antithetics; it is not layered on them

**Decision: under the Sobol sampler, antithetic pairing is off and cannot be turned on.**

Three reasons, in descending order of how much they bind:

1. A digitally scrambled Sobol net is *already* balanced — every elementary interval in base 2
   holds its proportional share of points. Appending the mirror `1 − u` of half the net adds no
   balance the net does not have, while in general destroying the `(t, m, s)`-net property that
   motivated Sobol. Antithetics buys its reduction from a symmetry the net has already exploited.
2. **The two error estimators are incompatible objects.** The antithetic estimator is a pair-mean
   SE over `n_pairs` independent pairs (`models/estimator.py`); the RQMC estimator is a
   between-replicate SE over `R` independent scrambles. Composing them means nesting pair means
   inside replicates — more machinery, and *two* ways to select the wrong formula instead of one.
3. Under inverse transform the antithetic mirror of a scrambled net point frequently lands near an
   existing net point, so half the path budget re-samples regions the net already covers.

**Enforcement is structural, in three layers, two of which were free.** `SobolRandomSource.antithetic`
is a `@property` returning `False`, not a field — it cannot be constructed otherwise. `simulate()`'s
**pre-existing** `engine.antithetic != rng.antithetic` guard then forces `EngineConfig(antithetic=False)`
for every QMC run. `price_rqmc` re-validates at its entry point so the caller gets the message there
rather than from inside scipy.

Rejected: "antithetic Sobol" (pair the first `n/2` net points with their mirrors), and exposing the
two as orthogonal flags on `EngineConfig` for the caller to combine. The second is precisely what
Amendment 4 meant by "two designs requiring reconciliation... left to whichever code lands last".

### 2. One Sobol point set, partitioned into per-stream column blocks by a declared layout

`RandomSource` is stateless and keyed only by stream *name* — that is what makes P2.M4's
common-random-number seam safe. Copying that design for Sobol (hashing the stream name into a
per-stream scramble) would give the `"variance"` and `"spot"` streams **identical or rank-correlated
point sets**: deterministically coupled draws, every price quietly wrong, and nothing raising.

`SobolRandomSource` therefore holds **one** point set of total dimension `D` and an explicit ordered
`dims` layout assigning each stream a disjoint contiguous column block. Measured cross-stream
correlation at n=4096: max |r| = 0.0007 (a layout bug makes this 1.0).

Consequences worth stating because they constrain later work: `n_paths` must be a power of two
(`random_base2` is the balanced API; `random(n)` at other counts silently surrenders equidistribution),
`D = 2·n_steps ≤ 21201` caps `n_steps` at 10600, and **the dimension count is a CRN boundary** — a
revaluation at a different `n_steps` gets a different net, so a multi-expiry P2.M4 portfolio needs one
point set per expiry.

**The CRN property survives the move to Sobol**, which is what `exo/CLAUDE.md` engine constraint #2
requires. The scramble derives from `(seed, replicate)` alone and from **no model parameter**, so a
bump-and-revalue pair sharing `(seed, replicate, dims, n_paths)` receives a bitwise-identical point
set. Beta is likewise passable explicitly so it can be frozen across a bump pair.

### 3. Brownian bridge: applied per stream, per scheme

| Scheme | Stream | Content | Bridged? |
|---|---|---|---|
| `euler-ft` | `variance` | Brownian increment | yes |
| `euler-ft` | `spot` | perpendicular Brownian increment | yes |
| `qe` | `variance` | *uniforms* feeding a branch selector and two inverse CDFs | **no** |
| `qe` | `spot` | normal driver scaled by a per-step, per-path diffusion coefficient | yes |

QE's variance stream is not a Brownian increment at all — there is no additive Gaussian structure to
reorder. QE's spot stream is bridged, but the construction is *approximate* there because the
diffusion coefficient varies by step and path, so the bridge's variance concentration is not exact.
That approximation was not asserted to be beneficial; it was measured (§5).

The bridge lives in `models/bridge.py` as a pure orthogonal reparametrization, verified by
`‖M·Mᵀ − I‖ ≈ 2.2e-16` on the identity input rather than by sampling, and `models/heston.py` is
**unmodified** by it. Note the bridge's "leading Sobol dimensions" rationale holds only *within* a
bridged stream's own columns — under `dims = (("variance", n), ("spot", n))` the bridged block starts
at global dimension `n_steps`. Reordering was measured immaterial at `n_steps=50` and deliberately
not done.

### 4. The between-replicate SE is the only valid RQMC error bar

RQMC points are **not** independent, so the plain sample standard error over a single scrambled net
is invalid — and optimistic, which is the unsafe direction: it makes the `0 < se < tol_abs` conjunct
of every gate in this package easier to pass. This is the structural twin of the pair-mean lesson
already recorded in `models/estimator.py`, except that one failed safe and this one does not.

**`mc_estimate` therefore raises on a low-discrepancy bundle**, and `PriceResult.combine` raises
across replicate results (pooling by raw path count would reconstruct exactly the outlawed formula).
There is no code path that produces a plain sample SE from a Sobol bundle. `rqmc_estimate` reports
`pv = mean(replicate means)` and `se = std(replicate means, ddof=1)/√R`, and raises below `R = 8`.

**A calibration finding that corrects this amendment's own first draft.** The replicate-mean
distribution is heavy-tailed where QMC works best, so the sample SD under-reads σ at small `R`.
Measured on a **d=4 smooth analytic integrand**: reported/true 0.48 at R=8, 0.76 at R=32, 0.92 at
R=128, with excess kurtosis rising from 12 to 418 as the net grows. **That regime is not this
package's.** At the real configuration (`d = 2·n_steps ≈ 100`, discontinuous payoffs) the study
measures RQMC calibration at **0.835–1.314** against pseudo-random controls at **0.876–1.331** in the
same run — indistinguishable. The formula SE is honest here. The low-dimensional table is retained
in `rqmc_estimate`'s docstring as a measured worst case, explicitly scoped, because it is the only
evidence for the `R ≥ 8` floor and the gates' `R = 64`. Outside both regimes the calibration is
unquantified; **P2.M4 is the trigger**, being the first milestone to publish `pv_std_err_e9`.

### 5. Measured results, and what they do NOT support

`T = 32` independent repetitions per (cell, configuration) at an identical `B = 32,768` paths, so
`2²⁰` paths per configuration-cell. The study ranks on the **empirically measured spread**
(`rmse = std(repetition pvs)`), never on formula SEs — comparing an honest pseudo-random SE against
an RQMC SE of unproven calibration would have inflated every ratio.

Ratio `rmse_pseudo / rmse_config`, ±13% (T−1 = 31 df):

| cell | pseudo+antithetic | rqmc | rqmc+bridge | +cv | outcome |
|---|---|---|---|---|---|
| vanilla/AAPL/qe | 1.41× | 2.96× | 2.83× | — | top-tied |
| vanilla/MSFT/qe | 1.55× | 2.87× | **5.53×** | — | ranked |
| barrier/AAPL/qe | 1.70× | 1.62× | 2.11× | 2.42× | top-tied (5-way) |
| barrier/MSFT/qe | 1.41× | 2.40× | 3.24× | 2.89× | top-tied |
| autocallable/AAPL/qe | 1.52× | 1.64× | 1.84× | — | top-tied |
| autocallable/MSFT/qe | 0.90× | 1.27× | 1.39× | — | top-tied |
| vanilla/AAPL/euler-ft | 1.43× | 1.83× | **2.56×** | — | ranked |

**What is established:** every RQMC configuration beats the plain pseudo-random denominator, 1.27×
to 5.53×. `rqmc+bridge` is best-or-tied-for-best in all seven cells and is never worse than the
antithetic baseline.

**What is NOT established, and must not be read into the table:** a best variant on five of seven
cells. Those are genuine ties at T=32 — resolving a 0.13 gap on a ~2.9 ratio needs T in the
thousands. `barrier/AAPL` is a five-way tie that **includes `pseudo+antithetic`**, so RQMC is not
demonstrably better than the antithetics already in the engine at that cell.

**The denominator matters for reading this table.** Every ratio is against *plain* pseudo-random,
but `EngineConfig.antithetic` already defaults to `True`, so the baseline a production caller would
switch away from is `pseudo+antithetic`. Dividing those out, `rqmc+bridge` is **1.21× to 3.57×**
better than the real baseline — and on the tied cells that margin is not resolvable at this T.

Two independent cross-checks passed. The antithetic baseline reproduces Amendment 4's separately
measured 1.3937 (QE) / 1.3715 (Euler) at **1.41× / 1.43×**. And the honest-direction results were
reported rather than adjusted: `pseudo+antithetic` at **0.90×** on autocallable/MSFT (antithetics
actively hurting) and bridge contribution at **0.96×** on vanilla/AAPL (a wash).

**Bridge contribution** (`rmse(rqmc)/rmse(rqmc+bridge)`) ranges 0.96× to 1.92×, positive on six of
seven cells. It earns its place, but not uniformly.

### 6. Control variates: Heston vanilla, for barriers only, with beta supplied from outside

Amendment 4 named the Heston characteristic-function vanilla as the natural control for a barrier
under Heston, and directed it be built as a reusable pricer for exactly this. It is.

**Beta is never fitted on the sample it then prices.** `price_from_bundle(control=...)` *requires* an
explicit `beta`; `price_rqmc` fits it on one disjoint pilot replicate whose own price is discarded.
Same-sample OLS beta was rejected for the reason `PriceResult.combine`'s docstring already records
for a different mechanism — weights estimated from the data they weight — and, worse, the textbook
`n−2` degrees-of-freedom correction assumes independent samples and is simply **not valid under
RQMC**, so there is no correct small-sample fix available. A control variate with any *fixed* beta
costs variance when the beta is poor, never bias.

**A correctness caveat that survives into P2.M2/M4, and is not fixed here.** `vanilla_control` draws
its sample from the **discretized** model but takes its mean from the **exact-model** characteristic
function, so the estimator transfers `β·(E_disc − E_exact)`. Measured at 400k antithetic paths (AAPL
params, K = s0 = 187.50, T = 1): `−0.014` under QE/50, `−0.003` under QE/400, and **`+0.072` under
euler-ft/50 (z = +3.22)**. Under a mis-specified step count the control variate therefore **silently
repairs discretization bias rather than reporting it** — it would mask the exact euler-ft/50 defect
§7 records below. Documented at the call site with a ponytail marker; a discretized control mean is a
design change, not a remediation.

**Scope: barrier calls only.** No put control, no parity conversion, and **no autocallable control** —
its coupon and early-redemption structure has no closed form, and the roadmap's wording is "control
variates where a closed form exists".

**The measured contribution is weak under RQMC, and this fires the marker's own trigger.** CV under
RQMC: **1.15×** (barrier/AAPL) and **0.89×** (barrier/MSFT — it slightly hurt). Without RQMC: 1.26×
and 1.34×. QMC and the control variate remove overlapping variance, so stacking them returns much
less than either suggests alone. Both cells sit inside the five-way and four-way ties above, so even
these numbers are not individually resolved at T=32.

### 7. A defect in an existing gate, found by this slice and fixed in it

RQMC's tighter error bar made a pre-existing problem resolvable: **G1's `euler-ft` arm ran at
`n_steps=50`, which §4 of Amendment 4 already records as below full-truncation Euler's first-passing
step count of 104 at this Feller ratio (0.333).** Two independent measurements: at 200k pseudo paths
the bias against the characteristic function is `+0.058` (z = +3.68); across 20 seeds at 20,000
antithetic paths it passed 20/20 but at mean z = +1.02, max |z| = 2.73 — roughly 0.27σ from going red
on a seed change. G1 was returning PASS for a configuration that violates `exo/CLAUDE.md`'s own
blocking acceptance test.

G1's euler-ft arm now runs at `n_steps=104` (mean z = +0.19, max |z| = 1.97, 20/20), and `tol_abs`
was **not** touched. The general lesson is recorded because it will recur: **a more precise estimator
can turn a green gate red without anything having broken**, since "within 3 SE" is a claim about bias
relative to your error bar. This is also why the RQMC gates copy their tolerance literals verbatim
from their pseudo-random twins and let only the path budget float — a tolerance re-measured to fit a
new sampler is a weaker gate, and in a diff it is indistinguishable from a legitimate retune.

### 8. Sampler default: recommended, not yet switched

The question deferred to this study was whether Sobol becomes the production default the way QE
became the production default *scheme* in Amendment 4 §4. **It is not the same kind of decision.**
QE vs Euler is a *bias* question — the wrong choice yields wrong prices. Sobol vs pseudo-random is a
*variance* question — scrambled RQMC is unbiased either way, so the wrong choice yields correct
prices more slowly.

**Decision: `rqmc+bridge` is the recommended sampler on the evidence above, and pseudo-random with
antithetics is retained as an independently-tested cross-check — but the default is not switched in
this milestone, because P2.M1 has no production caller.** Nothing outside tests and studies invokes
the pricer; term sheets are fixtures and no number reaches the bus until P2.M4. The switch is
therefore P2.M4's to make, against its own portfolio-revaluation requirements, with two facts it must
weigh that this study supplies: `rqmc+bridge` is never worse than the antithetic baseline and up to
3.57× better, but on two cells that advantage is not resolvable at T=32; and `D = 2·n_steps` makes the
step count a CRN boundary for bump-and-revalue.

`EngineConfig` and `exo.toml` gain **no sampler knob** in this slice, deliberately — adding one now
would pre-commit that decision. `extra="forbid"` means a later optional field is purely additive.

## Consequences (Amendment 6)

- **No new dependency, no wire-format change, no component touched.** `scipy.stats.qmc` and
  `scipy.special.ndtri` were already inside the `scipy==1.15.*` pin, holding Amendment 4's
  "P2.M1 requires no new dependency". `protocol/` is untouched.
- **`RunManifest` schema goes to v2** (`sampler`, `n_replicates`, `bridge_streams`), reading v1 and
  v2. An RQMC run is not reproducible from `{scheme, n_steps, antithetic}` alone. No scramble-seed
  list is stored: replicate `i`'s scramble derives from `(seed, i)`, so `seed` + `n_replicates` +
  `sampler` reproduce the run exactly. Under RQMC `n_paths` means paths **per replicate**.
- **A gap this slice does not close:** `ValuationSnapshot.ProductLine` carries no sampler field, so a
  published RQMC price would be indistinguishable from a pseudo-random one on the live plane. No
  proto change is made here (it needs its own ADR, codegen and compat check, and nothing publishes
  yet). Owner: **P2.M4 / P3**, when EXO first publishes.
- **P2.M2 constraints.** Longstaff–Schwartz regresses across paths, so under RQMC the regression must
  run **inside each replicate** and paths must never be pooled across replicates — pooling would both
  invalidate the between-replicate SE and bias the continuation value, and no existing guard catches
  it. `PathBundle.low_discrepancy` is a required field precisely so an LSM bundle cannot be
  constructed fail-open.
- **The SPX carried risk is unchanged and keeps its existing owner.** This study swept AAPL (Feller
  0.333) and MSFT (1.633) only, per Amendment 5. `exo.toml [models.SPX]` at 0.231 remains **P2.M2's
  warrant gate's** trigger. This amendment does not re-assign it.
