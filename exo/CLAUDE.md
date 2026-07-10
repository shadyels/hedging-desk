# CLAUDE.md — exo (Python)

Exotics pricing and rehedging service. Computes theoretical values and Greeks for the structured-product book, derives per-book **target positions**, and publishes them to Delta One over NATS. EXO never sends orders, never talks to FIX or Kafka, and never nets — those are Delta One's jobs. Read the root `CLAUDE.md` first.

## Package layout (`src/exo/`)

| Package | Role |
|---------|------|
| `models/`   | dynamics + numerics: GBM, Heston, Heston-local-vol (LSV, P4), MC engine, QMC (Sobol + Brownian bridge), variance reduction, (PDE solver in a later milestone) |
| `products/` | payoff definitions: autocallable, barrier option, reverse convertible, barrier reverse convertible, bonus certificate, TARF, PTARF |
| `greeks/`   | bump-and-revalue with common random numbers; pathwise where implemented |
| `bus/`      | NATS client (official `nats-py`), Protobuf encode/decode, target publisher, fill/risk consumer |
| `hedger/`   | Tier-2 optimizer (ADR-009): vega/liquidity QP over the option chain → `HedgeProposal`; rates mapping (rho per ccy bucket → futures qty) → `InternalTransferRequest` + RATES-IR targets |
| `portfolio/`| position store per book, delta aggregation per underlying, rehedge trigger logic (band/threshold) |

## Product/model roadmap (do not skip ahead)

1. **M1:** Barrier option + Autocallable under Heston, equity underlyings.
2. **M2:** Reverse convertible, barrier reverse convertible, bonus certificate — same MC engine, new payoffs only. If M2 requires touching `models/`, the payoff abstraction is wrong; fix the abstraction.
3. **M3:** TARF and PTARF. These are FX products: same Heston-style dynamics on FX spot with domestic/foreign rate drift (Garman–Kohlhagen-style), monthly fixings, target-redemption knockout, path-dependent accumulated gain state. Calibration realism (FX smile) is explicitly out of demo scope — document the parameter set used, don't pretend it's calibrated.
4. **P4.M3–M4 (mandatory, ADR-009):** hedge proposal optimizer with its
   property test (recomputed post-exposure from legs matches the claim within
   MC error); Heston-local-vol leverage surface; PDE cross-check pricer;
   calibration framework against sim-generated synthetic vanilla surfaces.

Every priced product must have an analytic or semi-analytic cross-check test
where one exists (e.g., Heston vanilla via characteristic function; barrier
under Black–Scholes closed form with the model degenerated to BS). MC vs
closed-form agreement within 3 standard errors is the acceptance test.

## Non-negotiable rules

1. **Reproducibility:** every published number carries `(model_id, params_hash, seed, n_paths, git_sha)` in the Protobuf metadata. Seeds come from a run manifest, never from time.
2. **Vectorize, don't loop:** NumPy end-to-end; the MC engine simulates all paths as arrays. `numba` may be added for path-dependent state updates (TARF accumulation, barrier monitoring) — never hand-rolled Python loops over paths in production code.
3. **Floating point stays inside pricing.** At the bus boundary convert to the fixed-point integers defined in `protocol/proto/common.proto` (`price_e9`, `qty_e2`), rounding policy: half-even, documented in `bus/convert.py` with tests on boundary values.
4. **Targets, not orders.** The publisher emits `TargetPosition{book_id, instrument_id, target_qty_e2, as_of, valuation_meta}` on `exo.targets.<book>.<instrument>`. Rehedge banding (no-trade band around current delta) lives in `portfolio/`, is config-driven, and is included in the message metadata so Delta One and the UI can display *why* a target moved.
5. **Consume, reconcile, alert:** EXO subscribes to Delta One execution reports and position snapshots. If EXO's view of a book position diverges from Delta One's snapshot beyond tolerance, publish `exo.alerts.recon` and stop publishing new targets for that book until reconciled. Silent divergence is the worst failure mode this service has.
6. **Greeks (full monitored set per ADR-008: delta, gamma, vega, rho, theta, dividend sensitivity):** bump-and-revalue with common random numbers (same seed per bump pair); bump sizes per risk factor live in config, not code. Report standard errors alongside estimates. Never publish a Greek whose MC standard error exceeds the configured max without flagging it.

## Tooling & style

- Python 3.12+, `uv` for env/deps, `ruff` (lint+format), `mypy --strict`.
- Everything typed. Payoffs are frozen `dataclass`es; model params are
  `pydantic` models validated at load.
- `pytest`; numerical tests use fixed seeds and assert within tolerances that
  include the MC standard error — never exact float equality, never `==` on
  arrays.
- No pandas on the pricing path (fine in notebooks/analysis).
- Async: `bus/` uses `asyncio` + `nats-py`; pricing runs in a
  `ProcessPoolExecutor` so a heavy revaluation cannot stall the bus heartbeat.
- Config via a single `exo.toml` loaded at startup; no env-var spelunking in
  business logic.

## Performance posture (be honest in the demo)

EXO is throughput-oriented, not latency-oriented. Full-book revaluation cadence target: seconds, not microseconds. Do not micro-optimize Python; if a pricer is too slow, the answer is numba/vectorization or (later) moving that kernel to Rust behind the same product interface — not async tricks.
