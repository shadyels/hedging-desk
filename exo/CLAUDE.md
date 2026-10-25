# CLAUDE.md — exo (Python)

Exotics pricing and rehedging service. Computes theoretical values and Greeks for the structured-product book, derives per-book **target positions**, and publishes them to Delta One over NATS. EXO never sends orders, never talks to FIX or Kafka, and never nets — those are Delta One's jobs. Read the root `CLAUDE.md` first.

## Package layout (`src/exo/`)

| Package | Role |
|---------|------|
| `models/`   | dynamics + numerics: GBM, Heston, Heston-local-vol (LSV, P4), MC engine, QMC (Sobol + Brownian bridge), variance reduction, Longstaff–Schwartz early-exercise engine (`lsm.py`, built P2.M2, American warrants); Bermudan CRR reference (`binomial.py`) and measured peak-memory constants (`memory.py`), P2.M2; (PDE solver in a later milestone) |
| `products/` | **Abstraction:** payoffs are frozen dataclass term sheets implementing the `Payoff` Protocol, returning an undiscounted dated `CashflowLedger`; discounting lives in `products/pricer.py`, never inside a payoff; barrier monitoring modes (`CONTINUOUS_BRIDGE` / `DISCRETE`) live in `products/base.py`. **Built:** barrier option, Phoenix autocallable (P2.M1); reverse convertible, barrier reverse convertible, bonus certificate, warrant (call/put, European and American, the American case priced by `price_american`), mini future (long/short) (P2.M2); TARF, PTARF (P2.M3). The `Payoff` Protocol signature is `cashflows(bundle, *, r)`, with the discount rate threaded in (P2.M2). |
| `greeks/`   | bump-and-revalue with common random numbers (`bump.py`); no pathwise Greeks yet |
| `bus/`      | `convert.py` (Protobuf encode/decode) + `service.py` (`python -m exo`): NATS client (official `nats-py`), target publisher, fill/risk consumer |
| `hedger/`   | **Not built yet** (P4.M3, ADR-009). Planned: Tier-2 optimizer: vega/liquidity QP over the option chain → `HedgeProposal`; rates mapping (rho per ccy bucket → futures qty) → `InternalTransferRequest` + RATES-IR targets |
| `portfolio/`| `book.py`, `aggregate.py`, `revalue.py`, `state.py`: position store per book, delta aggregation per underlying, rehedge trigger logic (band/threshold) |

## Product/model roadmap (do not skip ahead)

1. **M1 (scope settled 2026-09-05, ADR-006 Amendment 4):** Barrier option + autocallable under Heston, equity underlyings. **Pricing and MC standard error only — no Greeks**; ADR-008's monitored set is consumed by target publication and lands with M4. Illustrative model parameters (`S0, r, q, v0, κ, θ, ξ, ρ` per underlying) live in an `exo.toml [models.*]` section, **never** in `protocol/refdata/universe.json` — EXO is their only consumer and they are explicitly uncalibrated, so the shared contract surface would lend them an authority they do not have. Product term sheets are `pytest` fixtures; there is no persisted product book until M4's portfolio loop needs one (M4 delivered `exo/book.toml`). The discretization scheme decision is resolved in ADR-006 Amendment 4 §4: **QE (Andersen 2008) is the production default scheme**, validated at Feller ratio 0.333 in a convergence study. Full-truncation Euler is retained as an independent cross-check / reference scheme and remains a fully valid, selectable, independently-tested production choice, just not the default.
2. **M2:** Reverse convertible, barrier reverse convertible, bonus certificate, warrants (call and put, European **and American** exercise), mini futures (long and short: open-ended, daily financing accrual, stop-loss reset, continuous knock-out, residual-value settlement) — same MC engine, new payoffs only. **The M2 abstraction rule governs payoff additions:** if a new *payoff* requires touching `models/`, the payoff abstraction is wrong; fix the abstraction. **Exercise style is the sanctioned exception** — American warrants need a Longstaff–Schwartz early-exercise engine, which is a numerics property and legitimately lives in `models/` (ADR-006 Amendment 3). Adding LSM does **not** license any other `models/` change in M2. ADR-006 Amendment 7 §1 carves out the gate-reference analytics and measured constants that P2.M2 added to `models/` (`bs_barrier_rebate_at_hit`, `models/binomial.py`, `models/memory.py`). LSM is low-biased (a lower bound on the American value) and must regress the continuation value on `(S, v)`, not on `S` alone.
3. **M3:** TARF and PTARF. These are FX products: same Heston-style dynamics on FX spot with domestic/foreign rate drift (Garman–Kohlhagen-style), monthly fixings, target-redemption knockout, path-dependent accumulated gain state. Calibration realism (FX smile) is explicitly out of demo scope — document the parameter set used, don't pretend it's calibrated.
4. **P4.M3–M4 (mandatory, ADR-009):** hedge proposal optimizer with its property test (recomputed post-exposure from legs matches the claim within MC error); Heston-local-vol leverage surface; PDE cross-check pricer; calibration framework against sim-generated synthetic vanilla surfaces.

Every priced product must have an analytic or semi-analytic cross-check test where one exists (e.g., Heston vanilla via characteristic function; barrier under Black–Scholes closed form with the model degenerated to BS). MC vs closed-form agreement within 3 standard errors is the acceptance test.

## Engine design constraints — decide these in M1 or re-cut the engine in M2/M4

These are not style preferences. Each one is invisible in M1's own deliverables and breaks a later
milestone if M1 chooses the locally simpler option. Full reasoning in `docs/ROADMAP.md` (P2.M1
risks) and ADR-006 Amendment 4.

1. **Retain the path matrix; do not build a forward-streaming engine.** Longstaff–Schwartz regresses
   the continuation value *backwards* over stored state at every exercise date, and per ADR-006
   Amendment 3 that state is `(S, v)`, not `S` alone. A memory-efficient engine that accumulates
   discounted payoffs and discards paths is the natural design under barrier + autocallable, and it
   makes M2's American warrants unimplementable without rewriting `models/` — which the M2 rule
   above forbids. Amendment 3 licenses *adding* LSM to `models/`; it does not license rebuilding the
   engine underneath it.
2. **RNG construction is an explicit input to pricing, not a private detail of path generation.**
   Rule 6 below mandates bump-and-revalue with common random numbers (same seed per bump pair). M1
   computes no Greeks, but if it hides the generator, M4 cannot hand a bumped revaluation the
   identical normals. Deferring Greeks defers the estimator, not its plumbing.
3. **A price and its standard error are one return value.** `ValuationSnapshot.ProductLine.pv_std_err_e9`
   (`protocol/proto/live.proto`) is a **required** field — the protocol structurally enforces the
   3-standard-error acceptance test above, and a pricer that cannot report its own error bar cannot
   fill the message. Do not add error bars later.
4. **QMC and antithetics do not compose naïvely.** Antithetic pairing of a Sobol sequence destroys
   the low-discrepancy property Sobol was chosen for; the Brownian bridge is what makes Sobol
   effective, by concentrating variance in the leading dimensions. **Decided (ADR-006 Amendment 6
   §1): Sobol REPLACES antithetics.** `SobolRandomSource.antithetic` is a property fixed at `False`,
   so `simulate()`'s antithetic-mismatch guard rejects the combination. Under RQMC the only valid
   error bar is the between-replicate SE (Amendment 6 §4): `mc_estimate` raises on a
   `low_discrepancy` bundle and `PriceResult.combine` raises across replicates — price through
   `price_rqmc` / `rqmc_estimate`, never by pooling paths. `rqmc+bridge` is the recommended
   sampler but is not yet a default — no sampler knob exists; P2.M4 decided not to switch (ADR-006 Amendment 9 §1: published numbers use pseudo-random + antithetic).
5. **Build the characteristic-function vanilla as a reusable pricer, not a test helper.** There is no
   closed form for a barrier under Heston, so the natural control variate *is* the Heston vanilla —
   the same analytics as M1's blocking validation gate. Burying it in a test module means writing it
   twice.
6. **The reproducibility triple is implemented and live.** `exo/src/exo/provenance.py` implements the full capture: `git_sha()` returns the current commit sha (suffixed `-dirty` if the tree has changes), `params_hash(obj)` returns the sha256 hexdigest of a canonical JSON serialization (`sort_keys=True, separators=(",", ":"), allow_nan=False`), and `RunManifest` is a frozen dataclass holding the complete reproducibility record (`schema_version`, `run_id`, `created_ns`, `git_sha`, `model_id`, `params_hash`, `seed`, `n_paths`, `engine`, `params`); it reads from / writes to TOML at `exo/run-manifests/<run_id>.toml`. Schema v2 (P2.M1 slice 3) adds `[engine]` keys `sampler`, `n_replicates` and `bridge_streams` — an RQMC run is not reproducible without them; v1 files still read. Schema v3 (P2.M4) adds `[engine] n_steps_per_year` for book manifests. The service writes one manifest per real revaluation to `exo/run-manifests/live/` (git-ignored). A dirty tree is refused for publication (`publish_allowed`) unless `exo.toml [provenance] allow_dirty = true`. Real manifests: `exo/run-manifests/2026-09-07T21-10-10Z-scheme-convergence.toml` (v1) and `exo/run-manifests/2026-09-24T20-22-32Z-qmc-variance-reduction.toml` (v2). **A manifest's `git_sha` must stay reachable:** squash-merging the branch it ran on orphans that commit, so tag it (`provenance/<study>`, e.g. `provenance/qmc-variance-reduction`) before merging. Root `CLAUDE.md` invariant #7 makes a number without this metadata **invalid**.

## Non-negotiable rules

1. **Reproducibility:** every published number carries `(model_id, params_hash, seed, n_paths, git_sha)` in the Protobuf metadata. Seeds come from a run manifest, never from time.
2. **Vectorize, don't loop:** NumPy end-to-end; the MC engine simulates all paths as arrays. `numba` may be added for path-dependent state updates (TARF accumulation, barrier monitoring) — never hand-rolled Python loops over paths in production code.
3. **Floating point stays inside pricing.** At the bus boundary convert to the fixed-point integers defined in `protocol/proto/common.proto` (`price_e9`, `qty_e2`), rounding policy: half-even, documented in `bus/convert.py` with tests on boundary values.
4. **Targets, not orders.** The publisher emits `TargetPosition{book_id, instrument_id, target_qty_e2, as_of, valuation_meta}` on `exo.targets.<book>.<instrument>`. Rehedge banding (no-trade band around the last published target) lives in `portfolio/`, is config-driven (`[rehedge]`), and is published as `TargetPosition.band_qty_e2`. That field is **authoritative, not informational**: Delta One applies it as this book's external-order band (ADR-005 §2 as amended by ADR-014 §11), and the UI shows it with `reason` to explain *why* a target moved.
5. **Consume, reconcile, alert:** EXO subscribes to Delta One execution reports and position snapshots. If EXO's view of a book position diverges from Delta One's snapshot beyond tolerance, publish `exo.alerts.recon` and stop publishing new targets for that book until reconciled. Silent divergence is the worst failure mode this service has.
6. **Greeks (full monitored set per ADR-008: delta, gamma, vega, rho, theta, dividend sensitivity):** bump-and-revalue with common random numbers (same seed per bump pair); bump sizes per risk factor live in config, not code. Report standard errors alongside estimates. Never publish a Greek whose MC standard error exceeds the configured max without flagging it.

## Tooling & style

- Python 3.13+, `uv` for env/deps, `ruff` (lint+format), `mypy --strict`.
- Everything typed. Payoffs are frozen `dataclass`es; model params are `pydantic` models validated at load.
- `pytest`; numerical tests use fixed seeds and assert within tolerances that include the MC standard error — never exact float equality, never `==` on arrays.
- No pandas on the pricing path (fine in notebooks/analysis).
- Async: `bus/` uses `asyncio` + `nats-py`; pricing runs in a `ProcessPoolExecutor` so a heavy revaluation cannot stall the bus heartbeat.
- Config via a single `exo.toml` loaded at startup; no env-var spelunking in business logic.

## Performance posture (be honest in the demo)

EXO is throughput-oriented, not latency-oriented. Full-book revaluation cadence target: seconds, not microseconds. Do not micro-optimize Python; if a pricer is too slow, the answer is numba/vectorization or (later) moving that kernel to Rust behind the same product interface — not async tricks.
