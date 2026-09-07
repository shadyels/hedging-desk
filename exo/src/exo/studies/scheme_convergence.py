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
   this step count pass" check is a per-row bound (`|bias| < 3*shared_se`) and is unaffected by
   that correlation between rows.

2. **Peak memory per batch depends on `n_steps`, so the batch SIZE must too.** Peak memory for
   one batch is `n_paths_per_batch * (n_steps + 1) * _BYTES_PER_PATH_STEP` bytes, MEASURED (not
   hand-counted -- see `_BYTES_PER_PATH_STEP`'s own comment and
   `test_bytes_per_path_step_covers_measured_peak_with_tracemalloc`) against the real
   `simulate()` on its antithetic path, the only path this study runs. Getting this constant
   right took THREE tries (2026-09-06, two code-review rounds): first counting 2 arrays (`S`,
   `v` only), then 4 (adding the two draw matrices but missing that antithetic concatenation
   holds a 5th full-size array live at the moment of the `np.concatenate`), now `tracemalloc`-
   measured at 5. A FIXED `n_paths_per_batch` sized for a coarse step count blows the SAME
   ceiling at a fine one -- batching exists precisely to keep peak memory at one batch, and
   holding batch SIZE constant instead of peak memory constant defeats that at exactly the step
   counts where the path matrix is largest. `resolve_batch_plan` instead holds `paths_per_cell`
   (the quantity that actually determines the cell's SE) constant and derives `n_paths_per_batch`
   per `n_steps` to respect `max_batch_bytes` (default 800_000_000).

Batches are EQUAL-SIZE within a cell, deliberately: `PriceResult.combine()` pools by raw path
count (P0-4, code review 2026-09-06 -- see estimator.py's `combine()` docstring for why this
replaced an earlier inverse-variance-weighted version), which for equal-`n_paths` operands
reduces to a plain average -- equal-size batches keep that property exact rather than
approximate. Where `paths_per_cell` does not divide evenly by the memory-derived
`n_paths_per_batch`, `n_batches = ceil(paths_per_cell / n_paths_per_batch)` and the ACTUAL total
paths simulated (`n_batches * n_paths_per_batch`) is rounded UP from `paths_per_cell` rather than
left as one ragged, smaller final batch.

**`rank_schemes` needs its OWN minimum-precision conjunct, and an ABSOLUTE one is WRONG** (P0-2,
two corrections, 2026-09-06). Every other gate in this slice asserts `|bias| < 3*se` AND
`se < tol_abs` (a test that can pass by being imprecise is not a gate) -- `rank_schemes`
originally checked only the first, so a scheme with higher payoff variance could pass at a
COARSER step count and win the ranking BECAUSE it is less precise. The FIRST fix
(`se_tol_for_paths_per_cell`, an absolute bound derived from `paths_per_cell`) was itself wrong:
`se` does not depend on `n_steps` at all -- it is set by the payoff's variance, which varies with
strike and expiry. A fixed absolute `se_tol` therefore permanently excludes whichever
strike/expiry cells have intrinsically higher payoff variance (e.g. longer expiries), NO MATTER
how many steps are swept, and when EVERY row at EVERY step count fails that unsatisfiable bound
for BOTH schemes, `rank_schemes` fell through to its wall-time tie-break for both -- turning the
convergence study into a stopwatch reading (Euler is ~3x faster per step) and recording that as
if it were a convergence result. The CORRECTED rule (see `rank_schemes`' own docstring) scores
each row against `shared_se = min(se across the schemes present at that exact cell)` instead: a
scale grounded in what both schemes actually achieved AT THAT CELL, which no amount of step
refinement can help a scheme cheat by inflating its own `se`. A scheme that never passes on any
step count is excluded from the ranking outright; if NO scheme ever passes, `rank_schemes` raises
rather than falling back to wall time -- wall time may only break a tie between schemes that both
genuinely converged.

**The QE inadmissibility fallback fraction is reported, not merely counted** (P0-3, BLOCKER, code
review 2026-09-06): `PathBundle.qe_fallback_count` (heston.py) existed but was consumed by
nothing, making Amendment A1's stated upgrade trigger ("fallback fraction exceeding a threshold")
unobservable. `price_batched_multi_strike` aggregates it into `SweepCell.qe_fallback_fraction`,
and `render_report` gives it its own column.

Payoffs are computed INLINE (`np.exp(-r*T) * np.maximum(S[:, -1] - strike, 0.0)`) at every call
site in this module. Slice 1 ships NO payoff abstraction -- slice 2 designs `Payoff` against two
REAL products (barrier option, autocallable), which is the pressure ADR-006 Amendment 4 s3 says
exposes a bad abstraction. Do not DRY this three-line expression into `models/`.
"""

from __future__ import annotations

import argparse
import math
import sys
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
# {90, 100, 110} below lands as ITM/ATM/OTM for a call on both.
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
# Peak-memory ceiling per batch, in bytes. A round ~800 MB budget this study chooses for itself
# -- resolve_batch_plan sizes n_paths_per_batch per n_steps so no batch exceeds it. Below
# `models/heston.py`'s own ~2.02 GB ponytail figure (tracemalloc-measured, second code-review
# round 2026-09-06) for its antithetic-path peak at 200k paths x 252 steps -- a FIXED batch size
# at that figure is exactly what this module's docstring, fix 2, replaced.
DEFAULT_MAX_BATCH_BYTES = 800_000_000

_THIS_FILE = Path(__file__).resolve()
_REPO_ROOT = _THIS_FILE.parents[4]  # .../exo/src/exo/studies/ -> .../hedging-desk
_EXO_ROOT = _THIS_FILE.parents[3]  # .../hedging-desk/exo
DEFAULT_REPORT_PATH = _REPO_ROOT / "docs" / "studies" / "p2m1-scheme-convergence.md"
DEFAULT_MANIFEST_DIR = _EXO_ROOT / "run-manifests"

# Peak bytes per (path, step) on the ANTITHETIC path (the only path this study ever runs).
# heston.py's simulate() allocates S, v, and two draw matrices (QE: u, z; euler-ft: z_variance,
# z_spot) at (n_paths, n_steps[+1]) size, float64 -- FOUR arrays, 4*8 bytes -- but
# PseudoRandomSource._draw (rng.py) builds each draw via `np.concatenate([base, mirror])`, so at
# the peak (second draw, with S/v already allocated) base (half) + mirror (half) + the
# concatenated result (full) are ALL LIVE AT ONCE: that is a FIFTH full-size array's worth of
# memory, not four. Measured directly with tracemalloc against the real simulate() (both
# schemes, antithetic=True): peak/array ~= 5.02-5.08 (see
# test_bytes_per_path_step_covers_measured_peak_with_tracemalloc, which asserts against the
# allocator, not a hand re-count of the source -- a hand re-count got this wrong TWICE:
# originally counting 2 arrays (S, v only), then 4 (missing the concatenation overhead).
# Corrected 2026-09-06 (second code-review round) to 5*8; DO NOT restructure `_draw` to chase a
# lower number -- a correct 5 beats a clever 4.
_BYTES_PER_PATH_STEP = 5 * 8


@dataclass(frozen=True)
class SweepCell:
    """One (scheme, n_steps, param_set, strike, expiry) cell's batched MC result vs reference.

    `n_paths_per_batch`/`n_batches` are recorded per cell because `resolve_batch_plan` derives
    them from `n_steps` (see module docstring, fix 2): they vary across the step-count grid even
    though every cell targets the same `paths_per_cell`.

    `qe_fallback_fraction` (P0-3, code review 2026-09-06) is the fraction of (path, step) cells
    across this cell's batches where QE's martingale correction was inadmissible and fell back to
    the uncorrected K0 (see `heston.py`'s `_qe_step` ponytail marker); `None` for "euler-ft",
    which has no such correction to fall back from.
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
    qe_fallback_fraction: float | None


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
        # P2 (code review 2026-09-06): symmetry with the override branch's warning above. The
        # floor-of-2 minimum can itself exceed a sufficiently tiny max_batch_bytes -- unreachable
        # at production values, but should warn rather than silently proceed, same as the
        # explicit-override branch does.
        footprint = n_paths_per_batch * bytes_per_path
        if footprint > max_batch_bytes:
            print(
                f"WARNING: memory-derived n_paths_per_batch={n_paths_per_batch} at "
                f"n_steps={n_steps} still needs {footprint / 1e9:.2f} GB, over "
                f"--max-batch-bytes ({max_batch_bytes / 1e9:.2f} GB) -- the floor-of-2 minimum "
                "exceeds the budget at this n_steps.",
                flush=True,
            )

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
) -> tuple[dict[float, PriceResult], dict[float, float], float | None]:
    """Run `n_batches` EQUAL-SIZE batches of the PRODUCTION `simulate()`, ONE PER BATCH, sharing
    each batch's `PathBundle` across ALL `strikes` (module docstring, fix 1): `mc_estimate()` is
    called once per strike against that same bundle, and each strike's `PriceResult` is folded
    into its OWN `combine()` accumulator across batches. Peak memory stays at one batch; batch
    `b` draws from `_sub_seed(base_seed, b)`.

    Returns `(per-strike combined PriceResult, per-strike wall_time_s, qe_fallback_fraction)`.
    A strike's `wall_time_s` is its own `mc_estimate` time plus an EVEN SHARE of the batches'
    `simulate()` time -- that time serves every strike equally (one bundle, many payoffs), so it
    is amortized across them rather than attributed to one strike or double-counted across all of
    them. `qe_fallback_fraction` (P0-3, code review 2026-09-06) is the fraction of (path, step)
    cells across all batches where QE's martingale correction fell back to the uncorrected K0 --
    shared across every strike (it is a property of the bundle, not the payoff) -- or `None` for
    "euler-ft", which has no such diagnostic (`PathBundle.qe_fallback_count is None` there).
    """
    if n_batches < 1:
        raise ValueError(f"n_batches must be >= 1, got {n_batches}")
    if not strikes:
        raise ValueError("strikes must be non-empty")

    combined: dict[float, PriceResult | None] = dict.fromkeys(strikes)
    strike_time: dict[float, float] = dict.fromkeys(strikes, 0.0)
    sim_time_total = 0.0
    fallback_count_total = 0
    fallback_cells_total = 0
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
        if bundle.qe_fallback_count is not None:
            fallback_count_total += bundle.qe_fallback_count
            fallback_cells_total += n_paths_per_batch * n_steps

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

    qe_fallback_fraction = (
        fallback_count_total / fallback_cells_total if fallback_cells_total > 0 else None
    )
    return results, wall_time, qe_fallback_fraction


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
    # P2 (code review 2026-09-06): heston_vanilla_price depends on (param_set, strike, expiry)
    # only -- not on scheme or n_steps -- so caching it here turns len(schemes)*len(n_steps_grid)
    # repeated quadratures per (param_set, strike, expiry) into exactly one.
    reference_cache: dict[tuple[str, float, float], float] = {}
    start = time.perf_counter()
    for done, (scheme, n_steps, param_set_name, expiry) in enumerate(units, start=1):
        params = _PARAM_SETS[param_set_name]
        n_paths_per_batch, n_batches = resolve_batch_plan(
            n_steps, paths_per_cell, max_batch_bytes, n_paths_per_batch_override
        )
        results, wall_times, qe_fallback_fraction = price_batched_multi_strike(
            params, scheme, n_steps, expiry, strikes, n_paths_per_batch, n_batches, base_seed
        )
        for strike in strikes:
            result = results[strike]
            cache_key = (param_set_name, strike, expiry)
            if cache_key not in reference_cache:
                reference_cache[cache_key] = heston_vanilla_price(
                    params, strike, expiry, is_call=True
                )
            reference = reference_cache[cache_key]
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
                    qe_fallback_fraction=qe_fallback_fraction,
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


def _shared_se_per_cell(
    rows: Sequence[SweepCell],
) -> dict[tuple[int, str, float, float], tuple[float, int]]:
    """`shared_se[(n_steps, param_set, strike, expiry)]` = `(smallest se, number of DISTINCT
    schemes)` among whichever scheme(s) are present in `rows` at that exact cell (P0-2, SECOND
    correction, 2026-09-06 -- see module docstring).

    This replaces an absolute `se_tol` derived from `paths_per_cell`, which was itself wrong:
    `se` is set by the payoff's variance (varies with strike/expiry), not by `n_steps` at all, so
    an absolute bound can permanently exclude high-variance cells no matter how many steps are
    swept -- exactly what happened in the definitive run (every T=2.0 row failed an
    unsatisfiable `se_tol` for BOTH schemes, and `rank_schemes` fell through to a wall-time
    tie-break). Scoring against the SMALLER of the schemes actually present at a cell grounds
    the bound in what was actually achieved there: a scheme cannot pass by being noisier than its
    own peer.

    The scheme COUNT is carried alongside the minimum (SHOULD-3, third code-review round,
    2026-09-06) because `min` over a single scheme's own `se` (when only one scheme has a row at
    that exact cell) silently reverts to exactly the bound the shared-SE fix replaced. Consumers
    (`_first_passing_step`) must refuse to treat such a cell as a valid pass no matter how small
    `|bias|` looks relative to that degenerate scale.

    Refining `n_steps` does NOT shrink `se` (SHOULD-8, third code-review round, 2026-09-06 --
    this exact confusion produced the first bad fix): `se` is set by the batch's PATH COUNT and
    the payoff's variance, neither of which trends with `n_steps` here -- `resolve_batch_plan`'s
    per-cell path counts are NON-MONOTONIC in `n_steps` (at `DEFAULT_PATHS_PER_CELL`/
    `DEFAULT_MAX_BATCH_BYTES`: 3.20M, 4.62M, 3.40M, 3.24M, 3.24M, 3.21M, 3.21M across this
    study's `N_STEPS_GRID`, from `ceil` rounding against a fixed memory ceiling). What refining
    `n_steps` shrinks is the discretization BIAS. `shared_se` works regardless: it
    is a snapshot of what was actually achieved at each INDIVIDUAL cell, and a scheme's bias
    shrinking toward zero as steps refine is what lets it start passing `|bias| < 3*shared_se` --
    the bound does not need to tighten for that to happen.
    """
    se_by_cell_and_scheme: dict[tuple[int, str, float, float], dict[Scheme, float]] = {}
    for row in rows:
        cell = (row.n_steps, row.param_set, row.strike, row.expiry)
        se_by_cell_and_scheme.setdefault(cell, {})[row.scheme] = row.se
    return {
        cell: (min(per_scheme.values()), len(per_scheme))
        for cell, per_scheme in se_by_cell_and_scheme.items()
    }


def _first_passing_step(
    scheme_rows: Sequence[SweepCell],
    shared_se: Mapping[tuple[int, str, float, float], tuple[float, int]],
) -> tuple[int, float, int] | None:
    """The coarsest `n_steps` (from `scheme_rows`, one scheme's rows only) at which EVERY row
    recorded at that step count (every param_set / strike / expiry combination present, with
    both param sets required to be represented) satisfies `|bias| < 3 * shared_se[cell]`, AND
    every one of those cells has at least 2 distinct schemes contributing to `shared_se` (SHOULD-3:
    a cell with only 1 scheme present degenerates `shared_se` to that scheme's own `se`, which is
    exactly the bound the shared-SE fix replaced -- such a cell can never count toward a pass).

    Returns `(n_steps, total wall_time_s of the rows at that n_steps, total paths simulated at
    that n_steps -- n_paths_per_batch * n_batches, identical across the group since both are
    derived from n_steps alone)`, or `None` if no step count in `scheme_rows` passes -- this
    scheme has not converged on this grid. The total-paths figure exists (SHOULD-9, third
    code-review round, 2026-09-06) so a cost-to-accuracy comparison between two schemes'
    different first-passing step counts can note when they simulated unequal path counts
    (`resolve_batch_plan`'s `ceil` rounding), which matters for how conservative the comparison
    is.
    """
    by_steps: dict[int, list[SweepCell]] = {}
    for row in scheme_rows:
        by_steps.setdefault(row.n_steps, []).append(row)

    for n_steps in sorted(by_steps):
        group = by_steps[n_steps]
        if len({row.param_set for row in group}) < 2:
            continue  # both param sets must be represented at this step count
        cell_scales = [
            shared_se[(row.n_steps, row.param_set, row.strike, row.expiry)] for row in group
        ]
        if any(n_schemes < 2 for _se, n_schemes in cell_scales):
            continue  # shared_se degenerates to one scheme's own se at some cell here
        if all(abs(row.bias) < 3.0 * se for row, (se, _n) in zip(group, cell_scales, strict=True)):
            total_paths = group[0].n_paths_per_batch * group[0].n_batches
            return n_steps, sum(row.wall_time_s for row in group), total_paths
    return None


class InconclusiveRankingError(ValueError):
    """Raised by `rank_schemes` when the swept grid cannot support a scheme decision: either no
    scheme converged at any step count, or every scheme converged at the COARSEST step count
    swept (no discriminating power -- see `rank_schemes`' docstring). Carries `rows` (SHOULD-7,
    third code-review round, 2026-09-06) so a caller -- `run_study` -- can still preserve the
    sweep as evidence (a rendered report) instead of losing a potentially hour-long run with
    nothing to show for it. Subclasses `ValueError` so existing `pytest.raises(ValueError, ...)`
    call sites keep working unchanged.
    """

    def __init__(self, message: str, rows: Sequence[SweepCell]) -> None:
        super().__init__(message)
        self.rows = rows


def rank_schemes(rows: Sequence[SweepCell]) -> Scheme:
    """The scheme whose `|bias|` first falls inside `3 * shared_se` (see `_shared_se_per_cell`)
    at the COARSEST step count, across BOTH parameter sets, tie-broken on wall time.

    A scheme that never passes at any step count in `rows` is EXCLUDED from the ranking, not
    penalized with an infinite key that still competes on wall time -- wall time may only ever
    break a tie between schemes that BOTH genuinely converged. This function raises
    `InconclusiveRankingError` (a `ValueError`) in TWO cases, rather than silently falling back
    to ranking by wall time, because a fallback that quietly becomes the entire decision is a
    defect in its own right:

    1. If NO scheme passes at any step count (P0-2, second fix, 2026-09-06) -- exactly what let
       a stopwatch reading (Euler is ~3x faster per step than QE) masquerade as a convergence
       result in the definitive run, when every T=2.0 row failed an unsatisfiable absolute
       `se_tol` for BOTH schemes.
    2. If EVERY scheme first-passes at the COARSEST step count swept (SHOULD-2, third
       code-review round, 2026-09-06) -- `shared_se = min(se)` closes INTER-scheme gaming (a
       noisier scheme cannot buy passage relative to its peer), but does not stop BOTH schemes
       being imprecise AT ONCE: if `shared_se` is loose enough that every scheme's bias already
       falls inside it at the coarsest step, the grid never had a chance to discriminate between
       them, and ranking by wall time from there is the SAME failure as case 1, reached from the
       other side. Increasing `paths_per_cell` (shrinking `shared_se`) is the fix, not a finer
       step grid -- see `_shared_se_per_cell`'s docstring for why refining `n_steps` does not
       shrink `se`.

    Requires at least two schemes present in `rows`: `shared_se` is a scale one scheme's own
    precision cannot inflate away, which only means something when there is a peer to compare
    against.

    Strike rows at a step count are priced off a SHARED `PathBundle` (module docstring, fix 1)
    and are therefore correlated with each other. That does not affect this check: "ALL rows
    pass" is a conjunction of independent per-row bounds, not a comparison BETWEEN rows, so
    correlation between them changes nothing about what this function computes.
    """
    schemes = sorted({row.scheme for row in rows})
    if not schemes:
        raise ValueError("rank_schemes requires at least one sweep row")
    if len(schemes) < 2:
        raise ValueError(
            "rank_schemes requires at least two schemes present in `rows`: shared_se is a scale "
            "computed ACROSS schemes at each cell, and needs a peer to compare against"
        )

    shared_se = _shared_se_per_cell(rows)
    coarsest_n_steps = min(row.n_steps for row in rows)

    best_scheme: Scheme | None = None
    best_key: tuple[float, float] | None = None
    first_pass_n_steps: dict[Scheme, int] = {}
    for scheme in schemes:
        scheme_rows = [row for row in rows if row.scheme == scheme]
        passing = _first_passing_step(scheme_rows, shared_se)
        if passing is None:
            continue  # never converges on this grid -- excluded, not ranked via an infinite key
        n_steps, wall_time, _total_paths = passing
        first_pass_n_steps[scheme] = n_steps
        key = (float(n_steps), wall_time)
        if best_key is None or key < best_key:
            best_key = key
            best_scheme = scheme

    if best_scheme is None:
        raise InconclusiveRankingError(
            "no scheme converged on this grid: every scheme failed |bias| < 3*shared_se at "
            "every step count swept. This is not rankable by wall time -- it means the grid "
            "needs finer steps, more paths (to shrink shared_se), or both.",
            rows,
        )

    if len(first_pass_n_steps) == len(schemes) and all(
        n_steps == coarsest_n_steps for n_steps in first_pass_n_steps.values()
    ):
        raise InconclusiveRankingError(
            "the sweep has no discriminating power: EVERY scheme first-passes at the coarsest "
            f"step count swept (n_steps={coarsest_n_steps}). shared_se exceeds the biases being "
            "resolved, so this grid decides nothing -- ranking by wall time from here would be "
            "the same failure this round already fixed once, reached from the other side. "
            "Increase paths_per_cell to shrink shared_se.",
            rows,
        )

    return best_scheme


@dataclass(frozen=True)
class ConvergenceCost:
    """Per-scheme convergence cost, for the artifact's cost-to-accuracy comparison (P0-2, fix 3,
    2026-09-06): the coarsest step count at which a scheme first passes `rank_schemes`' shared-SE
    bar, the wall time MEASURED at that step count, the resulting cost per step, and the total
    paths simulated at that step count (SHOULD-9, third code-review round, 2026-09-06 -- two
    schemes' first-passing step counts can simulate UNEQUAL total paths, since
    `resolve_batch_plan`'s `ceil` rounding is a function of `n_steps`; recording it lets the
    report state which direction that skews a given cost comparison). `None` fields mean the
    scheme never converged on the swept grid.
    """

    scheme: Scheme
    first_passing_n_steps: int | None
    wall_time_s_at_passing: float | None
    cost_per_step_s: float | None
    total_paths_at_passing: int | None


def convergence_costs(rows: Sequence[SweepCell]) -> list[ConvergenceCost]:
    """Per-scheme `ConvergenceCost`, using the same `shared_se`/`_first_passing_step` rule
    `rank_schemes` uses to choose a winner. This is what lets the rendered report show WHY a
    scheme was chosen, not merely which one was: the interesting number is not "scheme X is
    slower per step" but the RATIO of total cost each scheme actually pays to first reach the
    shared-SE bar -- a scheme that needs far fewer steps can still win even if each of its steps
    costs more (see the module docstring's QE-vs-Euler numbers).
    """
    schemes = sorted({row.scheme for row in rows})
    shared_se = _shared_se_per_cell(rows)
    costs: list[ConvergenceCost] = []
    for scheme in schemes:
        scheme_rows = [row for row in rows if row.scheme == scheme]
        passing = _first_passing_step(scheme_rows, shared_se)
        if passing is None:
            costs.append(
                ConvergenceCost(
                    scheme=scheme,
                    first_passing_n_steps=None,
                    wall_time_s_at_passing=None,
                    cost_per_step_s=None,
                    total_paths_at_passing=None,
                )
            )
        else:
            n_steps, wall_time, total_paths = passing
            costs.append(
                ConvergenceCost(
                    scheme=scheme,
                    first_passing_n_steps=n_steps,
                    wall_time_s_at_passing=wall_time,
                    cost_per_step_s=wall_time / n_steps,
                    total_paths_at_passing=total_paths,
                )
            )
    return costs


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


def _render_optional_int(value: int | None) -> str:
    return str(value) if value is not None else "unknown"


def render_report(
    rows: Sequence[SweepCell],
    chosen: Scheme | None,
    antithetic_reduction: Mapping[str, float],
    manifest: RunManifest,
    *,
    antithetic_reduction_n_steps: int | None = None,
    antithetic_reduction_n_paths: int | None = None,
) -> str:
    """Render the study's markdown artifact (written to `docs/studies/p2m1-scheme-convergence.md`
    by `main()`). The illustrative/uncalibrated disclaimer is the FIRST content line, so the
    scheme decision is never read out of context.

    Recomputes `convergence_costs(rows)` internally (P0-2, fix 3, 2026-09-06) so the artifact
    states not just WHICH scheme was chosen but WHY: each scheme's first all-pass step count,
    its measured cost there, and the resulting cost-to-accuracy ratio against the chosen scheme.

    `chosen=None` (SHOULD-7, third code-review round, 2026-09-06) means `rank_schemes` raised
    `InconclusiveRankingError` -- either no scheme converged, or the grid had no discriminating
    power (every scheme first-passed at the coarsest step swept). The report still renders IN
    FULL in that case -- sweep table and cost-to-accuracy comparison included -- with a "NO
    SCHEME CONVERGED" banner in place of a chosen-scheme line, so a caller can preserve the
    evidence of a (potentially hour-long) run before failing loudly, rather than losing it.

    `antithetic_reduction_n_steps`/`antithetic_reduction_n_paths` (SHOULD-9, third code-review
    round, 2026-09-06) name the `(n_steps, n_paths)` of the cell the antithetic-reduction ratios
    below were measured at; without them those two ratios are not reproducible from the artifact
    alone. Optional (default `None`, rendered as "unknown") only so existing callers/tests that
    predate this field are not forced to supply it.
    """
    costs = {cost.scheme: cost for cost in convergence_costs(rows)}
    chosen_cost = costs[chosen] if chosen is not None else None
    if chosen is None:
        chosen_line = (
            "**NO SCHEME CONVERGED on this grid.** `rank_schemes` raised "
            "`InconclusiveRankingError`: either no scheme passed `|bias| < 3*shared_se` at any "
            "step count, or every scheme passed at the COARSEST step count swept (the grid has "
            "no discriminating power -- `shared_se` exceeds the biases being resolved). The "
            "ADR-006 Amendment 4 s4 decision CANNOT be made from this run as configured. The "
            "sweep table below preserves the raw evidence; see the Cost-to-accuracy comparison "
            "for what each scheme actually achieved."
        )
    else:
        chosen_line = (
            f"**Chosen scheme: `{chosen}`** (fills ADR-006 Amendment 4 s4, previously PENDING)."
        )
    lines: list[str] = [
        "# P2.M1 Slice 1 -- Scheme Convergence Study (QE vs full-truncation Euler)",
        "",
        _DISCLAIMER,
        "",
        chosen_line,
        "",
        "## Provenance",
        "",
        f"- base seed: `{manifest.seed}`",
        f"- target paths per cell: `{manifest.n_paths}` (actual per-cell total varies slightly "
        "by n_steps -- see `n_paths_per_batch`/`n_batches` per row below, module docstring fix 2)",
        f"- git_sha: `{manifest.git_sha}`",
        f"- params_hash: `{manifest.params_hash}`",
        f"- run_id: `{manifest.run_id}`",
        "- NOTE: `engine.n_steps` in the manifest reflects only the FINEST grid point swept "
        "(the manifest schema has one `n_steps` field; this study sweeps seven) -- see the "
        "sweep table below for the full grid.",
        "",
        "## Ranking rule",
        "",
        "The scheme whose `|bias| < 3 * shared_se` first holds for EVERY row (both param sets, "
        "every strike/expiry) at a given step count, at the COARSEST such step count, tie-broken "
        "on wall time -- where `shared_se` at a cell is the SMALLEST `se` among the schemes "
        "present there, so a scheme cannot pass by being noisier than its own peer. A scheme "
        "that never passes at any step count is excluded from the ranking; if NO scheme passes "
        "at any step count, this run would have raised rather than silently ranking by wall "
        "time -- see `rank_schemes`' docstring for the full rule and why an earlier revision's "
        "absolute `se_tol` was wrong.",
        "",
        "## Cost-to-accuracy comparison",
        "",
        'The interesting number is not "which scheme is faster per step" but the RATIO of '
        "total wall-clock cost each scheme actually pays to first reach the shared-SE bar above "
        "-- a scheme needing far fewer steps can win even when each of its steps costs more. "
        "`total_paths` is the actual path count simulated at that scheme's first-passing "
        "`n_steps` (SHOULD-9, third code-review round, 2026-09-06): compared schemes can "
        "simulate UNEQUAL totals here, since `resolve_batch_plan`'s `ceil` rounding is a "
        "function of `n_steps` -- when the scheme with the FEWER total paths still wins (a "
        "larger `total_paths` inflates that scheme's own wall time, working against it), the "
        "comparison is CONSERVATIVE, not favorable to the winner.",
        "",
        "| scheme | first all-pass n_steps | wall_time_s at that n_steps | total_paths | "
        "cost_per_step_s | cost vs chosen |",
        "|---|---|---|---|---|---|",
    ]
    for scheme in sorted(costs):
        cost = costs[scheme]
        if cost.first_passing_n_steps is None:
            lines.append(f"| {scheme} | never converged on this grid | | | | |")
            continue
        assert cost.wall_time_s_at_passing is not None
        assert cost.cost_per_step_s is not None
        chosen_wall_time = chosen_cost.wall_time_s_at_passing if chosen_cost is not None else None
        if chosen_wall_time is not None and chosen_wall_time > 0.0:
            cost_ratio = f"{cost.wall_time_s_at_passing / chosen_wall_time:.2f}x"
        else:
            cost_ratio = ""
        lines.append(
            f"| {scheme} | {cost.first_passing_n_steps} | {cost.wall_time_s_at_passing:.3f} | "
            f"{cost.total_paths_at_passing} | {cost.cost_per_step_s:.6f} | {cost_ratio} |"
        )
    lines += [
        "",
        "## Antithetic variance reduction achieved",
        "",
        "`se_plain / se_antithetic`, per scheme, at one representative cell (feller_violating, "
        "ATM, T=0.5y or the sweep's first expiry, "
        f"n_steps=`{_render_optional_int(antithetic_reduction_n_steps)}`, "
        f"n_paths=`{_render_optional_int(antithetic_reduction_n_paths)}` "
        "-- named explicitly, SHOULD-9 third code-review round 2026-09-06, so these two ratios "
        "are reproducible from the artifact alone). A ratio > 1 is a genuine reduction; a ratio "
        "< 1 means antithetics made the estimator WORSE -- a real, reportable possibility under "
        "QE (mirroring the variance draw is a valid antithetic but its reduction is not "
        "guaranteed), recorded here as a finding, not hidden as a bug.",
        "",
        "| scheme | se_plain / se_antithetic |",
        "|---|---|",
    ]
    for scheme_name, ratio in antithetic_reduction.items():
        lines.append(f"| {scheme_name} | {ratio:.4f} |")
    lines += [
        "",
        "## Sweep results",
        "",
        "Strike rows at a given (scheme, n_steps, param_set, expiry) share one `PathBundle` "
        "(common random numbers across strikes -- module docstring fix 1), so their biases are "
        "directly comparable rather than differing partly by sampling noise. `qe_fallback_frac` "
        "is the fraction of (path, step) cells where QE's martingale correction fell back to "
        "the uncorrected K0 (Amendment A1); blank for euler-ft, which has no such fallback.",
        "",
        "| scheme | n_steps | param_set | strike | expiry | pv | se | bias | bias/se | "
        "wall_time_s | n_paths_per_batch | n_batches | qe_fallback_frac |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        fallback_str = (
            f"{row.qe_fallback_fraction:.4%}" if row.qe_fallback_fraction is not None else ""
        )
        lines.append(
            f"| {row.scheme} | {row.n_steps} | {row.param_set} | {row.strike} | {row.expiry} | "
            f"{row.pv:.5f} | {row.se:.5f} | {row.bias:+.5f} | {row.bias_over_se:+.2f} | "
            f"{row.wall_time_s:.3f} | {row.n_paths_per_batch} | {row.n_batches} | "
            f"{fallback_str} |"
        )
    lines.append("")
    return "\n".join(lines)


@dataclass(frozen=True)
class StudyResult:
    rows: list[SweepCell]
    chosen_scheme: Scheme | None
    antithetic_reduction: dict[str, float]
    antithetic_reduction_n_steps: int
    antithetic_reduction_n_paths: int
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

    If `rank_schemes` raises `InconclusiveRankingError` (SHOULD-7, third code-review round,
    2026-09-06 -- no scheme converged, or the grid has no discriminating power), this function
    does NOT propagate the exception: it returns a `StudyResult` with `chosen_scheme=None`
    instead, with every other field (rows, antithetic measurement, manifest) still populated, so
    `main()` can render and write the sweep as evidence before failing loudly rather than losing
    a potentially hour-long run with nothing to show for it. `rank_schemes` itself is unaffected
    and still raises for callers that invoke it directly.
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

    try:
        chosen: Scheme | None = rank_schemes(rows)
    except InconclusiveRankingError:
        chosen = None

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
        # When chosen is None (ranking was inconclusive), schemes[0] is a MEANINGLESS
        # placeholder here -- the manifest schema has no "no decision" representation for this
        # field. render_report's "NO SCHEME CONVERGED" banner is the actual signal, not this.
        engine=EngineSettings(
            scheme=chosen if chosen is not None else schemes[0],
            n_steps=n_steps_grid[-1],
            antithetic=True,
        ),
        params=params_payload,
    )
    return StudyResult(
        rows=rows,
        chosen_scheme=chosen,
        antithetic_reduction=antithetic_reduction,
        antithetic_reduction_n_steps=representative_n_steps,
        antithetic_reduction_n_paths=representative_n_paths,
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
        result.rows,
        result.chosen_scheme,
        result.antithetic_reduction,
        result.manifest,
        antithetic_reduction_n_steps=result.antithetic_reduction_n_steps,
        antithetic_reduction_n_paths=result.antithetic_reduction_n_paths,
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

    if result.chosen_scheme is None:
        # SHOULD-7 (third code-review round, 2026-09-06): the report and manifest above are
        # ALREADY WRITTEN -- the sweep is preserved as evidence -- so failing loudly here loses
        # nothing. Failing loudly and preserving evidence are not in tension.
        print(
            "NO SCHEME CONVERGED on this grid -- see the written report for the raw sweep "
            "evidence and the Cost-to-accuracy comparison for what each scheme achieved.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    print(f"chosen scheme: {result.chosen_scheme}")


if __name__ == "__main__":
    main()
