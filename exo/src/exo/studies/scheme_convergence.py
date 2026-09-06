"""P2.M1 slice 1's discretization-scheme convergence study (ADR-006 Amendment 4 s4, PENDING):
QE (Andersen 2008) vs full-truncation Euler (Lord, Kahl & Jackel 2010), swept over step count,
under a Feller-satisfying and a Feller-violating parameter set, against `heston_vanilla_price` as
reference. Run with `python -m exo.studies.scheme_convergence`; writes
`docs/studies/p2m1-scheme-convergence.md`.

THE PARAMETERS SWEPT HERE ARE ILLUSTRATIVE AND UNCALIBRATED (ADR-006 Amendment 4 s1) -- the scheme
choice this study makes is validated under these numbers, not a universal claim; the artifact
states this up front for exactly that reason.

**Batching is the load-bearing design decision, not an optimization.** A planning spike ran this
sweep at n_paths=200_000 under a Feller-violating set and found the SE (+/-0.020) the SAME SIZE as
the bias being measured (0.009-0.035): every cell passed a 3-SE test and the Euler column was
NON-MONOTONE in step count -- the sweep was reading its own noise. Driving SE below the bias needs
roughly 16x the paths. So each sweep cell runs many batches through the PRODUCTION `simulate()` +
`mc_estimate()`, folded with `PriceResult.combine()` -- the engine under test is never a
study-only streaming variant.

**Two things batching must get right, both fixed here after being wrong in an earlier revision:**

1. **Share one `PathBundle` across every strike.** `simulate()`'s cost does not depend on
   `strike` -- only the payoff does. Re-running `simulate()` once per strike (three times, for
   `STRIKES`) tripled the entire sweep's compute for nothing. `price_batched_multi_strike` now
   simulates ONCE per batch and calls `mc_estimate` once per strike against that SAME bundle,
   folding each strike's own `PriceResult` into its own `combine()` accumulator across batches.
   This is not a compromise, it is an improvement: the three strike rows at a given step count
   then sit on COMMON RANDOM NUMBERS, so their biases differ only by the payoff's actual
   strike-dependence, not partly by independent sampling noise between strikes. It does mean
   strike rows at one step count are correlated with each other; `rank_schemes`' "ALL rows at
   this step count pass" check is a per-row bound (`|bias| < 3*se`) and is unaffected by that
   correlation between rows.

2. **Peak memory per batch depends on `n_steps`, so the batch SIZE must too.** Peak memory for
   one batch is `n_paths_per_batch * (n_steps + 1) * 2 arrays * 8 bytes` (`S` and `v`, float64 --
   `models/heston.py`'s `PathBundle`, whose own ponytail marker documents a ~810 MB ceiling at
   200k paths x 252 steps). A FIXED `n_paths_per_batch` sized for that 252-step figure blows the
   same ceiling 4x over at `n_steps=1008` (200k x 1009 x 2 x 8 = 3.23 GB) -- batching exists
   precisely to keep peak memory at one batch, and holding batch SIZE constant instead of peak
   memory constant defeats that at exactly the step counts where the path matrix is largest.
   `resolve_batch_plan` instead holds `paths_per_cell` (the quantity that actually determines the
   cell's SE) constant and derives `n_paths_per_batch` per `n_steps` to respect
   `max_batch_bytes` (default 800_000_000, just under the 810 MB figure above).

Batches are EQUAL-SIZE within a cell, deliberately: `PriceResult.combine()`'s inverse-variance
weighting reduces exactly to a plain average when both operands' `std_err` are equal. Where
`paths_per_cell` does not divide evenly by the memory-derived `n_paths_per_batch`,
`n_batches = ceil(paths_per_cell / n_paths_per_batch)` and the ACTUAL total paths simulated
(`n_batches * n_paths_per_batch`) is rounded UP from `paths_per_cell` rather than left as one
ragged, smaller final batch.

Payoffs are computed INLINE (`np.exp(-r*T) * np.maximum(S[:, -1] - strike, 0.0)`) at every call
site in this module. Slice 1 ships NO payoff abstraction -- slice 2 designs `Payoff` against two
REAL products (barrier option, autocallable), which is the pressure ADR-006 Amendment 4 s3 says
exposes a bad abstraction. Do not DRY this three-line expression into `models/`.
"""

from __future__ import annotations

import argparse
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from exo.models.estimator import PriceResult, mc_estimate
from exo.models.heston import simulate
from exo.models.heston_cf import heston_vanilla_price
from exo.models.params import EngineConfig, HestonParams, Scheme
from exo.models.rng import PseudoRandomSource
from exo.provenance import EngineSettings, RunManifest, git_sha, params_hash

# Mirrors exo.toml's [models.AAPL] (Feller-violating) / [models.MSFT] (Feller-satisfying)
# illustrative, UNCALIBRATED texture (ADR-006 Amendment 4 s1) -- kept as LOCAL constants rather
# than loaded from exo.toml so this study's grid does not silently drift if exo.toml's numbers are
# later tweaked for an unrelated reason. s0=100 (not AAPL's/MSFT's actual spot) so the strike grid
# {90, 100, 110} below lands as OTM/ATM/ITM for both.
FELLER_VIOLATING = HestonParams(
    s0=100.0, r=0.02, q=0.01, v0=0.04, kappa=1.5, theta=0.04, xi=0.6, rho=-0.7
)  # feller_ratio = 2*1.5*0.04/0.6**2 = 0.333 < 1
FELLER_SATISFYING = HestonParams(
    s0=100.0, r=0.02, q=0.01, v0=0.05, kappa=2.0, theta=0.05, xi=0.35, rho=-0.55
)  # feller_ratio = 2*2.0*0.05/0.35**2 = 1.633 >= 1

_PARAM_SETS: dict[str, HestonParams] = {
    "feller_satisfying": FELLER_SATISFYING,
    "feller_violating": FELLER_VIOLATING,
}

SCHEMES: tuple[Scheme, ...] = ("qe", "euler-ft")
N_STEPS_GRID: tuple[int, ...] = (4, 12, 52, 104, 252, 504, 1008)
STRIKES: tuple[float, ...] = (90.0, 100.0, 110.0)
EXPIRIES: tuple[float, ...] = (0.5, 2.0)

# Fixed constant, NOT derived from wall-clock time (exo/CLAUDE.md rule 1) -- an arbitrary but
# reproducible base seed for this study's runs.
DEFAULT_SEED = 20260906
# TOTAL paths per cell (across however many batches it takes) -- the quantity that actually
# determines a cell's SE. ~16x a single 200k-path batch, per the planning spike in this module's
# docstring.
DEFAULT_PATHS_PER_CELL = 3_200_000
# Peak-memory ceiling per batch, in bytes. Just under `models/heston.py`'s own ~810 MB ponytail
# figure for its (S, v) path matrices at 200k paths x 252 steps.
DEFAULT_MAX_BATCH_BYTES = 800_000_000

_THIS_FILE = Path(__file__).resolve()
_REPO_ROOT = _THIS_FILE.parents[4]  # .../exo/src/exo/studies/ -> .../hedging-desk
_EXO_ROOT = _THIS_FILE.parents[3]  # .../hedging-desk/exo
DEFAULT_REPORT_PATH = _REPO_ROOT / "docs" / "studies" / "p2m1-scheme-convergence.md"
DEFAULT_MANIFEST_DIR = _EXO_ROOT / "run-manifests"

_BYTES_PER_PATH_STEP = 2 * 8  # S and v, float64


@dataclass(frozen=True)
class SweepCell:
    """One (scheme, n_steps, param_set, strike, expiry) cell's batched MC result vs reference.

    `n_paths_per_batch`/`n_batches` are recorded per cell because `resolve_batch_plan` derives
    them from `n_steps` (see module docstring, fix 2): they vary across the step-count grid even
    though every cell targets the same `paths_per_cell`.
    """

    scheme: Scheme
    n_steps: int
    param_set: str
    strike: float
    expiry: float
    pv: float
    se: float
    bias: float
    bias_over_se: float
    wall_time_s: float
    n_paths_per_batch: int
    n_batches: int


def _sub_seed(base_seed: int, batch: int) -> int:
    """Deterministic per-batch seed derived from `(base_seed, batch)`, NOT from wall-clock time.

    Feeding both integers into one `SeedSequence`'s entropy pool (rather than, say,
    `base_seed + batch`) avoids two nearby base seeds colliding on the same batch index's stream.
    """
    return int(np.random.SeedSequence([base_seed, batch]).generate_state(1)[0])


def _discounted_call_payoff(
    r: float, expiry: float, strike: float, s_t: NDArray[np.float64]
) -> NDArray[np.float64]:
    # INLINE per this module's docstring: no Payoff abstraction exists in slice 1.
    result: NDArray[np.float64] = np.exp(-r * expiry) * np.maximum(s_t - strike, 0.0)
    return result


def resolve_batch_plan(
    n_steps: int,
    paths_per_cell: int,
    max_batch_bytes: int,
    n_paths_per_batch_override: int | None = None,
) -> tuple[int, int]:
    """Resolve `(n_paths_per_batch, n_batches)` for one `n_steps` value (module docstring, fix 2).

    Peak memory for one batch is `n_paths_per_batch * (n_steps + 1) * _BYTES_PER_PATH_STEP`.
    `n_paths_per_batch` is derived so that footprint stays at or under `max_batch_bytes`, floored
    to an EVEN number (antithetic requires it, `EngineConfig`'s own validator enforces it) and at
    least 2; `n_batches = ceil(paths_per_cell / n_paths_per_batch)`, so batches stay EQUAL-SIZE
    and the actual total (`n_batches * n_paths_per_batch`) rounds `paths_per_cell` UP rather than
    leaving one smaller, ragged final batch.

    `n_paths_per_batch_override`, if given, is used directly (still floored even) instead of the
    memory-derived value -- an explicit escape hatch used at the caller's own risk: a warning is
    printed if it would exceed `max_batch_bytes` at this `n_steps`.
    """
    bytes_per_path = (n_steps + 1) * _BYTES_PER_PATH_STEP
    if n_paths_per_batch_override is not None:
        n_paths_per_batch = n_paths_per_batch_override - (n_paths_per_batch_override % 2)
        n_paths_per_batch = max(n_paths_per_batch, 2)
        footprint = n_paths_per_batch * bytes_per_path
        if footprint > max_batch_bytes:
            print(
                f"WARNING: --n-paths-per-batch={n_paths_per_batch} at n_steps={n_steps} needs "
                f"{footprint / 1e9:.2f} GB, over --max-batch-bytes "
                f"({max_batch_bytes / 1e9:.2f} GB) -- proceeding anyway (explicit override).",
                flush=True,
            )
    else:
        max_paths_by_memory = max_batch_bytes // bytes_per_path
        n_paths_per_batch = min(paths_per_cell, max_paths_by_memory)
        n_paths_per_batch -= n_paths_per_batch % 2
        n_paths_per_batch = max(n_paths_per_batch, 2)

    n_batches = math.ceil(paths_per_cell / n_paths_per_batch)
    return n_paths_per_batch, n_batches


def price_batched_multi_strike(
    params: HestonParams,
    scheme: Scheme,
    n_steps: int,
    expiry: float,
    strikes: Sequence[float],
    n_paths_per_batch: int,
    n_batches: int,
    base_seed: int,
) -> tuple[dict[float, PriceResult], dict[float, float]]:
    """Run `n_batches` EQUAL-SIZE batches of the PRODUCTION `simulate()`, ONE PER BATCH, sharing
    each batch's `PathBundle` across ALL `strikes` (module docstring, fix 1): `mc_estimate()` is
    called once per strike against that same bundle, and each strike's `PriceResult` is folded
    into its OWN `combine()` accumulator across batches. Peak memory stays at one batch; batch
    `b` draws from `_sub_seed(base_seed, b)`.

    Returns `(per-strike combined PriceResult, per-strike wall_time_s)`. A strike's `wall_time_s`
    is its own `mc_estimate` time plus an EVEN SHARE of the batches' `simulate()` time -- that
    time serves every strike equally (one bundle, many payoffs), so it is amortized across them
    rather than attributed to one strike or double-counted across all of them.
    """
    if n_batches < 1:
        raise ValueError(f"n_batches must be >= 1, got {n_batches}")
    if not strikes:
        raise ValueError("strikes must be non-empty")

    combined: dict[float, PriceResult | None] = dict.fromkeys(strikes)
    strike_time: dict[float, float] = dict.fromkeys(strikes, 0.0)
    sim_time_total = 0.0
    for batch in range(n_batches):
        engine = EngineConfig(
            scheme=scheme,
            n_steps=n_steps,
            n_paths=n_paths_per_batch,
            expiry=expiry,
            antithetic=True,
        )
        rng = PseudoRandomSource(seed=_sub_seed(base_seed, batch))
        t0 = time.perf_counter()
        bundle = simulate(params, engine, rng)
        sim_time_total += time.perf_counter() - t0

        for strike in strikes:
            t1 = time.perf_counter()
            discounted_payoff = _discounted_call_payoff(params.r, expiry, strike, bundle.S[:, -1])
            batch_result = mc_estimate(bundle, discounted_payoff)
            strike_time[strike] += time.perf_counter() - t1
            existing = combined[strike]
            combined[strike] = batch_result if existing is None else existing.combine(batch_result)

    shared_share = sim_time_total / len(strikes)
    results: dict[float, PriceResult] = {}
    wall_time: dict[float, float] = {}
    for strike in strikes:
        result = combined[strike]
        assert result is not None  # n_batches >= 1 is enforced above
        results[strike] = result
        wall_time[strike] = shared_share + strike_time[strike]
    return results, wall_time


def run_sweep(
    *,
    paths_per_cell: int = DEFAULT_PATHS_PER_CELL,
    max_batch_bytes: int = DEFAULT_MAX_BATCH_BYTES,
    n_paths_per_batch_override: int | None = None,
    base_seed: int = DEFAULT_SEED,
    schemes: Sequence[Scheme] = SCHEMES,
    n_steps_grid: Sequence[int] = N_STEPS_GRID,
    strikes: Sequence[float] = STRIKES,
    expiries: Sequence[float] = EXPIRIES,
    verbose: bool = True,
) -> list[SweepCell]:
    """Sweep `schemes x n_steps_grid x {feller_satisfying, feller_violating} x expiries`, pricing
    every `strike` off one shared `PathBundle` per unit via `price_batched_multi_strike` (module
    docstring, fix 1). Grid arguments default to the full production sweep; tests pass tiny
    overrides to stay fast.

    Prints one progress line per unit when `verbose=True` (the default), with an ETA extrapolated
    from the mean time per unit completed so far: a multi-hour run that prints nothing is
    indistinguishable from a hung one.
    """
    units = [
        (scheme, n_steps, param_set_name, expiry)
        for scheme in schemes
        for n_steps in n_steps_grid
        for param_set_name in _PARAM_SETS
        for expiry in expiries
    ]
    total_units = len(units)
    rows: list[SweepCell] = []
    start = time.perf_counter()
    for done, (scheme, n_steps, param_set_name, expiry) in enumerate(units, start=1):
        params = _PARAM_SETS[param_set_name]
        n_paths_per_batch, n_batches = resolve_batch_plan(
            n_steps, paths_per_cell, max_batch_bytes, n_paths_per_batch_override
        )
        results, wall_times = price_batched_multi_strike(
            params, scheme, n_steps, expiry, strikes, n_paths_per_batch, n_batches, base_seed
        )
        for strike in strikes:
            result = results[strike]
            reference = heston_vanilla_price(params, strike, expiry, is_call=True)
            bias = result.pv - reference
            bias_over_se = bias / result.std_err if result.std_err > 0.0 else math.inf
            rows.append(
                SweepCell(
                    scheme=scheme,
                    n_steps=n_steps,
                    param_set=param_set_name,
                    strike=strike,
                    expiry=expiry,
                    pv=result.pv,
                    se=result.std_err,
                    bias=bias,
                    bias_over_se=bias_over_se,
                    wall_time_s=wall_times[strike],
                    n_paths_per_batch=n_paths_per_batch,
                    n_batches=n_batches,
                )
            )

        if verbose:
            elapsed = time.perf_counter() - start
            mean_per_unit = elapsed / done
            eta = mean_per_unit * (total_units - done)
            print(
                f"[{done}/{total_units}] scheme={scheme} n_steps={n_steps} "
                f"param_set={param_set_name} expiry={expiry} "
                f"n_paths_per_batch={n_paths_per_batch} n_batches={n_batches} | "
                f"elapsed={elapsed:.1f}s ETA={eta:.1f}s",
                flush=True,
            )
    return rows


def rank_schemes(rows: Sequence[SweepCell]) -> Scheme:
    """The scheme whose `|bias|` first falls inside 3 SE at the COARSEST step count, across BOTH
    parameter sets, tie-broken on wall time.

    Concretely: group each scheme's rows by `n_steps`. A step count "passes" for a scheme when,
    across every row recorded at that step count (every param_set / strike / expiry combination
    present, with both param sets required to be represented), `|bias| < 3*se` holds for ALL of
    them. Each scheme's rank key is `(smallest passing n_steps, else +inf; total wall time of the
    rows at that n_steps)`; schemes are ordered by that key ascending -- a coarser passing step
    count wins outright, and summed wall time at that step count breaks a tie between schemes that
    first pass at the same step count.

    Strike rows at a step count are now priced off a SHARED `PathBundle` (module docstring, fix
    1) and are therefore correlated with each other. That does not affect this check: "ALL rows
    pass" is a conjunction of independent per-row bounds (`|bias| < 3*se`), not a comparison
    BETWEEN rows, so correlation between them changes nothing about what this function computes.
    """
    schemes = sorted({row.scheme for row in rows})
    if not schemes:
        raise ValueError("rank_schemes requires at least one sweep row")

    best_scheme: Scheme | None = None
    best_key: tuple[float, float] | None = None
    for scheme in schemes:
        scheme_rows = [row for row in rows if row.scheme == scheme]
        by_steps: dict[int, list[SweepCell]] = {}
        for row in scheme_rows:
            by_steps.setdefault(row.n_steps, []).append(row)

        key: tuple[float, float] = (math.inf, sum(row.wall_time_s for row in scheme_rows))
        for n_steps in sorted(by_steps):
            group = by_steps[n_steps]
            if len({row.param_set for row in group}) < 2:
                continue  # both param sets must be represented at this step count
            if all(abs(row.bias) < 3.0 * row.se for row in group):
                key = (float(n_steps), sum(row.wall_time_s for row in group))
                break

        if best_key is None or key < best_key:
            best_key = key
            best_scheme = scheme

    assert best_scheme is not None  # schemes is non-empty, so the loop assigns at least once
    return best_scheme


def measure_antithetic_reduction(
    params: HestonParams,
    scheme: Scheme,
    n_steps: int,
    expiry: float,
    strike: float,
    n_paths: int,
    seed: int,
) -> float:
    """`se_plain / se_antithetic` for one independent MC run at each setting (same total
    `n_paths`, same scheme/params/n_steps/strike/expiry, independently seeded).

    A ratio > 1 means antithetics reduced variance; a ratio < 1 means antithetics made the
    estimator WORSE. That is a real, reportable possibility under QE -- mirroring the variance
    draw (`u -> 1-u` feeding `ndtri`) is a valid antithetic, but its variance reduction is not
    guaranteed -- and this study's artifact records it as a finding, not a bug to hide.
    """
    engine_antithetic = EngineConfig(
        scheme=scheme, n_steps=n_steps, n_paths=n_paths, expiry=expiry, antithetic=True
    )
    bundle_antithetic = simulate(
        params, engine_antithetic, PseudoRandomSource(seed=seed, antithetic=True)
    )
    payoff_antithetic = _discounted_call_payoff(
        params.r, expiry, strike, bundle_antithetic.S[:, -1]
    )
    se_antithetic = mc_estimate(bundle_antithetic, payoff_antithetic).std_err

    engine_plain = EngineConfig(
        scheme=scheme, n_steps=n_steps, n_paths=n_paths, expiry=expiry, antithetic=False
    )
    rng_plain = PseudoRandomSource(seed=seed + 1, antithetic=False)
    bundle_plain = simulate(params, engine_plain, rng_plain)
    payoff_plain = _discounted_call_payoff(params.r, expiry, strike, bundle_plain.S[:, -1])
    se_plain = mc_estimate(bundle_plain, payoff_plain).std_err

    return se_plain / se_antithetic


_DISCLAIMER = (
    "> **Illustrative and UNCALIBRATED parameters (ADR-006 Amendment 4 s1).** The Heston "
    "parameter sets swept in this study are demo/development values, not calibrated market "
    "data. The scheme choice recorded below is validated under THESE illustrative parameters; "
    "it is not a universal claim and must not be read as one."
)


def render_report(
    rows: Sequence[SweepCell],
    chosen: Scheme,
    antithetic_reduction: Mapping[str, float],
    manifest: RunManifest,
) -> str:
    """Render the study's markdown artifact (written to `docs/studies/p2m1-scheme-convergence.md`
    by `main()`). The illustrative/uncalibrated disclaimer is the FIRST content line, so the
    scheme decision is never read out of context."""
    lines: list[str] = [
        "# P2.M1 Slice 1 -- Scheme Convergence Study (QE vs full-truncation Euler)",
        "",
        _DISCLAIMER,
        "",
        f"**Chosen scheme: `{chosen}`** (fills ADR-006 Amendment 4 s4, previously PENDING).",
        "",
        "## Provenance",
        "",
        f"- base seed: `{manifest.seed}`",
        f"- target paths per cell: `{manifest.n_paths}` (actual per-cell total varies slightly "
        "by n_steps -- see `n_paths_per_batch`/`n_batches` per row below, module docstring fix 2)",
        f"- git_sha: `{manifest.git_sha}`",
        f"- params_hash: `{manifest.params_hash}`",
        f"- run_id: `{manifest.run_id}`",
        "",
        "## Antithetic variance reduction achieved",
        "",
        "`se_plain / se_antithetic`, per scheme, at one representative cell (feller_violating, "
        "ATM, T=0.5y or the sweep's first expiry). A ratio > 1 is a genuine reduction; a ratio "
        "< 1 means antithetics made the estimator WORSE -- a real, reportable possibility under "
        "QE (mirroring the variance draw is a valid antithetic but its reduction is not "
        "guaranteed), recorded here as a finding, not hidden as a bug.",
        "",
        "| scheme | se_plain / se_antithetic |",
        "|---|---|",
    ]
    for scheme, ratio in antithetic_reduction.items():
        lines.append(f"| {scheme} | {ratio:.4f} |")
    lines += [
        "",
        "## Sweep results",
        "",
        "Strike rows at a given (scheme, n_steps, param_set, expiry) share one `PathBundle` "
        "(common random numbers across strikes -- module docstring fix 1), so their biases are "
        "directly comparable rather than differing partly by sampling noise.",
        "",
        "| scheme | n_steps | param_set | strike | expiry | pv | se | bias | bias/se | "
        "wall_time_s | n_paths_per_batch | n_batches |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row.scheme} | {row.n_steps} | {row.param_set} | {row.strike} | {row.expiry} | "
            f"{row.pv:.5f} | {row.se:.5f} | {row.bias:+.5f} | {row.bias_over_se:+.2f} | "
            f"{row.wall_time_s:.3f} | {row.n_paths_per_batch} | {row.n_batches} |"
        )
    lines.append("")
    return "\n".join(lines)


@dataclass(frozen=True)
class StudyResult:
    rows: list[SweepCell]
    chosen_scheme: Scheme
    antithetic_reduction: dict[str, float]
    manifest: RunManifest


def run_study(
    *,
    paths_per_cell: int = DEFAULT_PATHS_PER_CELL,
    max_batch_bytes: int = DEFAULT_MAX_BATCH_BYTES,
    n_paths_per_batch_override: int | None = None,
    base_seed: int = DEFAULT_SEED,
    schemes: Sequence[Scheme] = SCHEMES,
    n_steps_grid: Sequence[int] = N_STEPS_GRID,
    strikes: Sequence[float] = STRIKES,
    expiries: Sequence[float] = EXPIRIES,
    verbose: bool = True,
) -> StudyResult:
    """Orchestrate the full study: sweep, rank, measure the antithetic finding, and build the
    `RunManifest`. `main()` renders and writes the artifact and manifest file; this function does
    no I/O beyond `git_sha()`'s `git` subprocess call, so tests can call it directly at tiny sizes.

    The manifest's `seed` field carries the base seed and its `n_paths` field carries the TARGET
    `paths_per_cell` -- the actual per-cell total varies slightly by `n_steps` (rounded up by
    `resolve_batch_plan`, module docstring fix 2), so the resolved `(n_paths_per_batch,
    n_batches)` per cell is recorded directly in the rendered report instead.
    """
    rows = run_sweep(
        paths_per_cell=paths_per_cell,
        max_batch_bytes=max_batch_bytes,
        n_paths_per_batch_override=n_paths_per_batch_override,
        base_seed=base_seed,
        schemes=schemes,
        n_steps_grid=n_steps_grid,
        strikes=strikes,
        expiries=expiries,
        verbose=verbose,
    )
    chosen = rank_schemes(rows)

    representative_n_steps = n_steps_grid[len(n_steps_grid) // 2]
    representative_strike = strikes[len(strikes) // 2]
    representative_expiry = expiries[0]
    representative_n_paths, _ = resolve_batch_plan(
        representative_n_steps, paths_per_cell, max_batch_bytes, n_paths_per_batch_override
    )
    antithetic_reduction: dict[str, float] = {
        scheme: measure_antithetic_reduction(
            FELLER_VIOLATING,
            scheme,
            representative_n_steps,
            representative_expiry,
            representative_strike,
            representative_n_paths,
            base_seed,
        )
        for scheme in schemes
    }

    params_payload: dict[str, dict[str, float]] = {
        name: params.model_dump() for name, params in _PARAM_SETS.items()
    }
    manifest = RunManifest(
        schema_version=1,
        run_id=f"{time.strftime('%Y-%m-%dT%H-%M-%SZ', time.gmtime())}-scheme-convergence",
        created_ns=time.time_ns(),
        git_sha=git_sha(),
        model_id="heston-scheme-convergence-v1",
        params_hash=params_hash(params_payload),
        seed=base_seed,
        n_paths=paths_per_cell,
        engine=EngineSettings(scheme=chosen, n_steps=n_steps_grid[-1], antithetic=True),
        params=params_payload,
    )
    return StudyResult(
        rows=rows,
        chosen_scheme=chosen,
        antithetic_reduction=antithetic_reduction,
        manifest=manifest,
    )


def main(argv: Sequence[str] | None = None) -> None:
    """`python -m exo.studies.scheme_convergence`: run the full production sweep and write
    `docs/studies/p2m1-scheme-convergence.md` plus a `RunManifest` under `exo/run-manifests/`.

    This is the EXPENSIVE real run (up to `DEFAULT_PATHS_PER_CELL` paths per cell, over the full
    scheme/step/param-set/expiry grid, sharing one `PathBundle` per unit across all strikes) --
    not exercised by CI; the fast smoke test (`exo/tests/test_scheme_convergence_smoke.py`) calls
    `run_study`/`run_sweep` directly with tiny overrides instead.
    """
    parser = argparse.ArgumentParser(
        prog="python -m exo.studies.scheme_convergence",
        description="P2.M1 slice 1 discretization-scheme convergence study "
        "(QE vs full-truncation Euler).",
    )
    parser.add_argument(
        "--paths-per-cell",
        type=int,
        default=DEFAULT_PATHS_PER_CELL,
        help="total paths per (scheme, n_steps, param_set, strike, expiry) cell -- determines SE",
    )
    parser.add_argument(
        "--max-batch-bytes",
        type=int,
        default=DEFAULT_MAX_BATCH_BYTES,
        help="peak-memory ceiling per batch; batch size is derived per n_steps to respect it",
    )
    parser.add_argument(
        "--n-paths-per-batch",
        type=int,
        default=None,
        help="explicit batch-size override, bypassing --max-batch-bytes (at your own risk)",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="base seed")
    parser.add_argument("--out", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--manifest-dir", type=Path, default=DEFAULT_MANIFEST_DIR)
    args = parser.parse_args(argv)

    result = run_study(
        paths_per_cell=args.paths_per_cell,
        max_batch_bytes=args.max_batch_bytes,
        n_paths_per_batch_override=args.n_paths_per_batch,
        base_seed=args.seed,
    )
    report = render_report(
        result.rows, result.chosen_scheme, result.antithetic_reduction, result.manifest
    )

    out_path: Path = args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report)

    manifest_dir: Path = args.manifest_dir
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / f"{result.manifest.run_id}.toml"
    result.manifest.write(manifest_path)

    print(f"wrote {out_path}")
    print(f"wrote {manifest_path}")
    print(f"chosen scheme: {result.chosen_scheme}")


if __name__ == "__main__":
    main()
