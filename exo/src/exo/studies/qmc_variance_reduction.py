"""P2.M1 slice 3's QMC variance-reduction study (T8): manually-invoked sweep measuring how much
RQMC (scrambled Sobol + Brownian bridge, ADR-006 Amendment 6), the bridge alone, and a control
variate each reduce Monte Carlo error relative to plain pseudo-random. Run with
`python -m exo.studies.qmc_variance_reduction`; writes
`docs/studies/p2m1-qmc-variance-reduction.md`. T9 (the orchestrator) runs the real sweep; this
module and its tests only prove it works.

**THE MEASUREMENT DESIGN IS NOT THE OBVIOUS ONE.** `rqmc_estimate`'s own docstring (and a
planning spike against scipy's Sobol) measured the between-replicate formula SE as optimistic by
an amount that GROWS with both replicate count R and net size n (the replicate-mean distribution
is heavy-tailed, excess kurtosis growing from ~12 at n=256 to ~418 at n=8192, at R=128). The
pseudo-random arms' formula SEs carry no such bias. Comparing the two formula SEs directly would
therefore inflate every RQMC-vs-pseudo ratio -- flattering the slice with nothing failing.

So this study never compares formula SEs. Instead, for each (cell, configuration): run `T`
independent REPETITIONS of that exact configuration, each at the identical total path budget `B`;
`rmse = std(repetition pvs, ddof=1)` is the honest error measure (free of the kurtosis bias,
`T-1` degrees of freedom); `mean_formula_se` (the mean of each repetition's own reported
`std_err`) is recorded too, and its ratio to `rmse` -- the SE calibration factor -- is measured on
THIS run's own configuration rather than borrowed from `rqmc_estimate`'s docstring table. The
headline ratio is `rmse_denominator / rmse_config`. Every ratio carries its own relative
precision, `1/sqrt(2*(T-1))` (~13% at T=32, since `rmse` is itself a sample standard deviation of
T draws) -- printed next to every ratio, never omitted.

**Degenerate cases raise, never fall back** (same posture as `scheme_convergence.rank_schemes`):
`rank_configs` raises `InconclusiveVarianceReductionError` if no configuration beats the
denominator (every ratio <= 1), or if every configuration ties within its own measurement
precision. A wall-time or best-effort fallback that returns a confident wrong answer is worse
than an error -- these are the same failure from opposite sides.

**Why the QE "variance" stream is never bridged.** Under QE (Andersen 2008), the "variance"
stream is UNIFORMS feeding a branch selector (exponential vs. quadratic) and, in the quadratic
branch, an inverse-CDF transform -- there is no additive Gaussian increment structure for the
Brownian bridge to reorder. Full-truncation Euler's "variance" stream, by contrast, IS an
additive Gaussian increment (like "spot" under both schemes), so it bridges. This is why
`_BRIDGE_BY_SCHEME` differs by scheme: `frozenset({"spot"})` under QE, `frozenset({"variance",
"spot"})` under euler-ft.

**Fixtures: AAPL and MSFT only (ADR-006 Amendment 5).** `exo.toml`'s SPX (Feller ratio 0.231) is
explicitly OUT of scope here -- it is P2.M2's warrant-gate trigger, unswept and unreassigned.
`euler-ft` is swept on the vanilla/AAPL cell only (enough to substantiate the per-scheme bridge
decision without doubling the run). AAPL/MSFT `HestonParams` are kept as LOCAL constants (mirrors
`scheme_convergence.py`) rather than loaded from `exo.toml`, so this study's grid does not
silently drift if `exo.toml` is later tweaked for an unrelated reason.

# ponytail: fixture ceiling -- bridge and control-variate ratios are measured ONLY at Feller
# ratios 0.333 (AAPL) and 1.633 (MSFT). Trigger: P2.M2's warrant gate, the existing owner of the
# SPX (0.231) sweep -- unchanged by this study.

**No new estimator, pricer, or payoff abstraction.** This module reuses `price`,
`price_from_bundle`, `price_rqmc`, `vanilla_control`, `estimate_beta`, `RunManifest`, `git_sha`,
`params_hash` unmodified. The "vanilla call" cells reuse `BarrierOption` with a barrier far
beyond reach (the same trick `test_pricer_rqmc.py` already uses, "avoids writing a second payoff
type just for these tests") rather than inventing a bespoke vanilla `Payoff`.

**No internal batching.** Unlike `scheme_convergence.py`, this study's default path budget per
repetition (`B = 32_768`) keeps one call's peak memory (~250 MB at defaults, `_PEAK_ARRAY_RATIO`
below) well under `--max-batch-bytes`'s 800 MB default, and only ONE bundle is ever live at a
time within a repetition (`price_rqmc`'s own design; the pseudo path allocates one bundle per
repetition too). `--max-batch-bytes` is therefore a REFUSAL guard, not a batching knob: a
(cell, configuration) whose estimated peak would exceed it raises rather than silently proceeding
or splitting itself into sub-batches -- reintroducing `resolve_batch_plan`'s machinery for a
sweep this small would be solving a problem this study does not have.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from exo.models.control_variate import estimate_beta, vanilla_control
from exo.models.heston import simulate
from exo.models.params import EngineConfig, HestonParams, Scheme
from exo.models.rng import PseudoRandomSource
from exo.products.autocallable import Autocallable
from exo.products.barrier import BarrierOption
from exo.products.base import Monitoring, Payoff, observation_indices
from exo.products.pricer import discount, price_from_bundle, price_rqmc
from exo.provenance import EngineSettings, RunManifest, git_sha, params_hash

# Mirrors exo.toml's [models.AAPL] / [models.MSFT] -- kept LOCAL (module docstring) rather than
# loaded from exo.toml so this study's grid does not silently drift.
_AAPL = HestonParams(
    s0=187.50, r=0.0425, q=0.0050, v0=0.0400, kappa=1.50, theta=0.0400, xi=0.60, rho=-0.70
)  # feller_ratio = 0.333
_MSFT = HestonParams(
    s0=420.00, r=0.0425, q=0.0080, v0=0.0500, kappa=2.00, theta=0.0500, xi=0.35, rho=-0.55
)  # feller_ratio = 1.633

_PARAMS: dict[str, HestonParams] = {"AAPL": _AAPL, "MSFT": _MSFT}

Product = Literal["vanilla", "barrier", "autocallable"]

_EXPIRY = 1.0
DEFAULT_N_STEPS = 50  # matches G3b/G4's own step count (test_validation_gates_products.py)

_BRIDGE_BY_SCHEME: dict[Scheme, frozenset[str]] = {
    "qe": frozenset({"spot"}),
    "euler-ft": frozenset({"variance", "spot"}),
}

DEFAULT_SEED = 20260924  # fixed literal, NOT derived from wall-clock time (exo/CLAUDE.md rule 1)
DEFAULT_REPETITIONS = 32  # T
DEFAULT_REPLICATES = 32  # R (RQMC scramble replicates per repetition)
DEFAULT_PATHS_PER_REPLICATE = 1024  # n (RQMC paths per replicate; power of two, required)
# B = DEFAULT_REPLICATES * DEFAULT_PATHS_PER_REPLICATE = 32_768 = 2**15: the identical total
# path budget every configuration at a cell uses per repetition (module docstring) -- for RQMC
# that is R replicates x n paths; for pseudo it is one B-path run.
DEFAULT_MAX_BATCH_BYTES = 800_000_000

_THIS_FILE = Path(__file__).resolve()
_REPO_ROOT = _THIS_FILE.parents[4]  # .../exo/src/exo/studies/ -> .../hedging-desk
_EXO_ROOT = _THIS_FILE.parents[3]  # .../hedging-desk/exo
DEFAULT_REPORT_PATH = _REPO_ROOT / "docs" / "studies" / "p2m1-qmc-variance-reduction.md"
DEFAULT_MANIFEST_DIR = _EXO_ROOT / "run-manifests"

# Peak bytes per (path, step) for one `price_rqmc` call (ONE replicate/bundle live at a time --
# price_rqmc's own design), MEASURED with tracemalloc (not hand-counted -- see
# `test_qmc_memory.py`) at the worst-case config this study actually runs: n_paths=1024,
# n_steps=50, scheme="qe", bridge={"spot"}, control_strike set (config 5's shape). Measured peak
# 7_660_768 bytes against a denominator of n_paths*(n_steps+1)*8 = 417_792 bytes -> ratio ~18.34
# (two repeated measurements agreed to 4 significant figures after a warmup call to exclude
# scipy's one-time lazy-import cost from the trace). Committed at 19.0, a ~3.6% margin over the
# measured value -- covers heston.py's own ~5x array footprint PLUS SobolRandomSource's
# (n_paths, 2*n_steps) point matrix PLUS bridge_normals' transient W array PLUS the control
# variate's own payoff/control arrays.
_PEAK_ARRAY_RATIO = 19.0


@dataclass(frozen=True)
class Cell:
    """One (product, underlying, scheme) sweep unit."""

    product: Product
    underlying: str
    scheme: Scheme

    @property
    def label(self) -> str:
        return f"{self.product}/{self.underlying}/{self.scheme}"


def _cells() -> tuple[Cell, ...]:
    """The full production cell grid (module docstring): QE on every (product, underlying), plus
    euler-ft on vanilla/AAPL only."""
    products: tuple[Product, ...] = ("vanilla", "barrier", "autocallable")
    cells = [
        Cell(product=product, underlying=underlying, scheme="qe")
        for product in products
        for underlying in ("AAPL", "MSFT")
    ]
    cells.append(Cell(product="vanilla", underlying="AAPL", scheme="euler-ft"))
    return tuple(cells)


def _vanilla_call(underlying: str, params: HestonParams) -> BarrierOption:
    """ATM call via a knock-out barrier set far beyond reach -- survival == 1 always, so the
    payoff is exactly `max(S_T - K, 0)` (same trick as `test_pricer_rqmc.py`'s `_vanilla_call`)."""
    return BarrierOption(
        underlying=underlying,
        option_type="call",
        strike=params.s0,
        expiry=_EXPIRY,
        barrier=params.s0 * 100.0,
        direction="up",
        knock="out",
        monitoring=Monitoring.CONTINUOUS_BRIDGE,
    )


def _down_and_out_call(underlying: str, params: HestonParams) -> BarrierOption:
    """G4-shaped down-and-out call (test_validation_gates_products.py), scaled to `params.s0`:
    ATM strike, barrier at 90% of strike (G4's own 90/100 ratio)."""
    return BarrierOption(
        underlying=underlying,
        option_type="call",
        strike=params.s0,
        expiry=_EXPIRY,
        barrier=0.9 * params.s0,
        direction="down",
        knock="out",
        monitoring=Monitoring.CONTINUOUS_BRIDGE,
    )


# Quarterly-ish observation schedule aligning EXACTLY onto the DEFAULT_N_STEPS=50 grid
# (0.2*50=10, 0.4*50=20, ..., 1.0*50=50 -- all integers; `observation_indices` raises
# `ScheduleAlignmentError` otherwise). A later change to `DEFAULT_N_STEPS` must keep this
# alignment or `_autocallable`'s own construction fails loudly at import-adjacent call time --
# `test_qmc_variance_reduction_smoke.py`'s
# `test_autocallable_observations_align_with_production_grid` pins this down explicitly.
_AUTOCALL_OBSERVATIONS: tuple[float, ...] = (0.2, 0.4, 0.6, 0.8, 1.0)


def _autocallable(underlying: str, params: HestonParams) -> Autocallable:
    """A GENUINELY LIVE Phoenix autocallable, spot-relative like `_down_and_out_call`.

    NOT G3b (test_validation_gates_products.py): G3b's own docstring says it plainly --
    "autocall_trigger/coupon_barrier set unreachably high (1e7) -- no path ever calls or earns
    a coupon." That is a DEGENERATE IDENTITY FIXTURE, built so the note collapses to a short put
    for a closed-form check. Copied into this study verbatim, the "autocallable" cell would have
    no early redemption and no coupons -- a European payoff on S_T, exactly the OPPOSITE of why
    this cell is in the study: the autocallable is the highest-dimensional, most discontinuous
    payoff in the slice, and exactly where RQMC's advantage is expected to degrade. A study that
    swept a degenerate autocallable would publish "RQMC handles autocallables fine" into an ADR
    on a payoff that never actually exercised its own discontinuity.

    `coupon_barrier=0.80*s0`, `autocall_trigger=1.00*s0`, `protection_barrier=0.70*s0`, five
    observations (`_AUTOCALL_OBSERVATIONS`) -- early redemption and coupons both fire on a real
    fraction of simulated paths at these levels (see `_measure_autocall_liveness`, reported in
    the rendered artifact's Cell results section, and asserted non-degenerate by
    `test_qmc_variance_reduction_smoke.py`)."""
    return Autocallable(
        underlying=underlying,
        notional=1000.0,
        observations=_AUTOCALL_OBSERVATIONS,
        autocall_trigger=1.00 * params.s0,
        coupon_barrier=0.80 * params.s0,
        coupon_rate=0.02,
        memory=True,
        protection_barrier=0.70 * params.s0,
        initial_level=params.s0,
        expiry=_EXPIRY,
    )


def _payoff_for(cell: Cell, params: HestonParams) -> Payoff:
    if cell.product == "vanilla":
        return _vanilla_call(cell.underlying, params)
    if cell.product == "barrier":
        return _down_and_out_call(cell.underlying, params)
    return _autocallable(cell.underlying, params)


@dataclass(frozen=True)
class AutocallLiveness:
    """Sanity-check that `_autocallable`'s term sheet is genuinely live at `underlying`'s real
    parameters -- coordinator correction: G3b's own fixture is deliberately degenerate (never
    calls, never earns a coupon), and swapping in a spot-relative live Phoenix without checking
    it is actually live would just move the degeneracy risk rather than remove it.

    `redeem_fraction[j]` / `coupon_fraction[j]` are measured over ALL paths (not just paths still
    active at observation j), so they are directly comparable across observations and sum
    sensibly: `sum(redeem_fraction) + never_redeemed_fraction == 1.0`.
    """

    underlying: str
    observations: tuple[float, ...]
    redeem_fraction: tuple[float, ...]
    coupon_fraction: tuple[float, ...]
    never_redeemed_fraction: float


def _measure_autocall_liveness(
    underlying: str, params: HestonParams, *, n_steps: int, n_paths: int, seed: int
) -> AutocallLiveness:
    """Simulate `n_paths` and replay `_autocallable`'s own coupon/autocall boolean logic against
    `bundle.S` at each observation (mirrors `Autocallable.cashflows`'s loop -- a read-only
    diagnostic over `_AUTOCALL_OBSERVATIONS` observations, not a per-path loop; exo/CLAUDE.md
    rule 2 governs loops over PATHS, which stay vectorized here) to report what fraction of
    paths redeem, and what fraction earn a coupon, at each observation date."""
    payoff = _autocallable(underlying, params)
    engine = EngineConfig(
        scheme="qe", n_steps=n_steps, n_paths=n_paths, expiry=_EXPIRY, antithetic=True
    )
    bundle = simulate(params, engine, PseudoRandomSource(seed=seed))
    obs_idx = observation_indices(np.asarray(payoff.observations, dtype=np.float64), bundle)

    active: NDArray[np.bool_] = np.ones(bundle.S.shape[0], dtype=np.bool_)
    redeem_fraction: list[float] = []
    coupon_fraction: list[float] = []
    for idx in obs_idx:
        s_j = bundle.S[:, idx]
        coupon_hit = active & (s_j >= payoff.coupon_barrier)
        autocall_hit = active & (s_j >= payoff.autocall_trigger)
        coupon_fraction.append(float(np.mean(coupon_hit)))
        redeem_fraction.append(float(np.mean(autocall_hit)))
        active = active & ~autocall_hit

    return AutocallLiveness(
        underlying=underlying,
        observations=payoff.observations,
        redeem_fraction=tuple(redeem_fraction),
        coupon_fraction=tuple(coupon_fraction),
        never_redeemed_fraction=float(np.mean(active)),
    )


@dataclass(frozen=True)
class ConfigSpec:
    """One of the 6 configurations compared at each cell (module docstring / brief table)."""

    config_id: int
    label: str
    sampler: Literal["pseudo", "rqmc"]
    antithetic: bool
    bridge: bool
    control_variate: bool
    barrier_only: bool


#              id  label                   sampler   anti.  bridge  cv     barrier_only
_CONFIGS: tuple[ConfigSpec, ...] = (
    ConfigSpec(1, "pseudo", "pseudo", False, False, False, False),
    ConfigSpec(2, "pseudo+antithetic", "pseudo", True, False, False, False),
    ConfigSpec(3, "rqmc", "rqmc", False, False, False, False),
    ConfigSpec(4, "rqmc+bridge", "rqmc", False, True, False, False),
    ConfigSpec(5, "rqmc+bridge+cv", "rqmc", False, True, True, True),
    ConfigSpec(6, "pseudo+antithetic+cv", "pseudo", True, False, True, True),
)


def _configs_for(cell: Cell) -> tuple[ConfigSpec, ...]:
    return tuple(c for c in _CONFIGS if not c.barrier_only or cell.product == "barrier")


def _sub_seed(base_seed: int, *tags: object) -> int:
    """Deterministic sub-seed derived from `(base_seed, *tags)`, NOT from wall-clock time
    (exo/CLAUDE.md rule 1). Each tag is hashed via its `str()` into an entropy contribution so
    cell labels, config labels and repetition indices all participate without colliding --
    mirrors `scheme_convergence._sub_seed`, extended to string tags."""
    entropy = [base_seed]
    for tag in tags:
        entropy.append(int.from_bytes(hashlib.sha256(str(tag).encode("utf-8")).digest()[:8], "big"))
    return int(np.random.SeedSequence(entropy).generate_state(1)[0])


def _estimate_peak_bytes(n_paths_this_call: int, n_steps: int) -> int:
    """Conservative peak-memory estimate for ONE `simulate()`/`price_rqmc()` call (module
    docstring's refusal-guard design)."""
    return int(_PEAK_ARRAY_RATIO * n_paths_this_call * (n_steps + 1) * 8)


def _fit_pseudo_beta(
    payoff: Payoff,
    params: HestonParams,
    scheme: Scheme,
    n_paths: int,
    n_steps: int,
    antithetic: bool,
    seed: int,
    control_strike: float,
) -> float:
    """Fit a control-variate beta from a pilot pseudo-random sample, DISJOINT from every
    estimation repetition (own `seed`) -- the same out-of-sample requirement
    `estimate_beta`'s docstring states, and the same pattern `price_rqmc` already applies
    internally for RQMC (this is that pattern's pseudo-random analogue, hand-rolled here
    because `price_from_bundle` -- unlike `price_rqmc` -- takes beta as a required external
    input and does no pilot fitting of its own)."""
    engine = EngineConfig(
        scheme=scheme, n_steps=n_steps, n_paths=n_paths, expiry=_EXPIRY, antithetic=antithetic
    )
    rng = PseudoRandomSource(seed=seed, antithetic=antithetic)
    bundle = simulate(params, engine, rng)
    discounted = discount(payoff.cashflows(bundle), r=params.r)
    control = vanilla_control(
        bundle, params, strike=control_strike, expiry=payoff.expiry, r=params.r
    )
    return estimate_beta(discounted, control.payoff)


@dataclass(frozen=True)
class _RawConfigMeasurement:
    rmse: float
    mean_formula_se: float
    repetitions: int


def _measure_config(
    cell: Cell,
    config: ConfigSpec,
    *,
    repetitions: int,
    replicates: int,
    paths_per_replicate: int,
    base_seed: int,
    max_batch_bytes: int,
    n_steps: int,
) -> _RawConfigMeasurement:
    """Run `repetitions` independent repetitions of `config` at cell `cell`, each at the
    identical total path budget `B = replicates * paths_per_replicate`, and return the honest
    `rmse` (module docstring) plus the mean of the repetitions' own formula SEs."""
    if repetitions < 2:
        raise ValueError(f"repetitions must be >= 2 to compute rmse (ddof=1), got {repetitions}")

    params = _PARAMS[cell.underlying]
    payoff = _payoff_for(cell, params)
    control_strike = params.s0 if config.control_variate else None

    n_paths_this_call = (
        replicates * paths_per_replicate if config.sampler == "pseudo" else paths_per_replicate
    )
    peak_estimate = _estimate_peak_bytes(n_paths_this_call, n_steps)
    if peak_estimate > max_batch_bytes:
        raise ValueError(
            f"{cell.label}/{config.label}: estimated peak {peak_estimate / 1e9:.2f} GB exceeds "
            f"--max-batch-bytes ({max_batch_bytes / 1e9:.2f} GB) -- this study refuses to run "
            "rather than batch within a repetition (module docstring); reduce --replicates/"
            "--paths-per-replicate or raise --max-batch-bytes."
        )

    beta: float | None = None
    if config.control_variate and config.sampler == "pseudo":
        assert control_strike is not None
        pilot_seed = _sub_seed(base_seed, cell.label, config.label, "pilot")
        beta = _fit_pseudo_beta(
            payoff,
            params,
            cell.scheme,
            n_paths_this_call,
            n_steps,
            config.antithetic,
            pilot_seed,
            control_strike,
        )

    pvs: list[float] = []
    ses: list[float] = []
    for rep in range(repetitions):
        seed = _sub_seed(base_seed, cell.label, config.label, rep)
        if config.sampler == "pseudo":
            engine = EngineConfig(
                scheme=cell.scheme,
                n_steps=n_steps,
                n_paths=n_paths_this_call,
                expiry=_EXPIRY,
                antithetic=config.antithetic,
            )
            rng = PseudoRandomSource(seed=seed, antithetic=config.antithetic)
            bundle = simulate(params, engine, rng)
            control = None
            if control_strike is not None:
                control = vanilla_control(
                    bundle, params, strike=control_strike, expiry=payoff.expiry, r=params.r
                )
            result = price_from_bundle(payoff, bundle, r=params.r, control=control, beta=beta)
        else:
            engine = EngineConfig(
                scheme=cell.scheme,
                n_steps=n_steps,
                n_paths=paths_per_replicate,
                expiry=_EXPIRY,
                antithetic=False,
            )
            bridge = _BRIDGE_BY_SCHEME[cell.scheme] if config.bridge else frozenset()
            result = price_rqmc(
                payoff,
                params,
                engine,
                seed=seed,
                n_replicates=replicates,
                bridge=bridge,
                control_strike=control_strike,
            )
        pvs.append(result.pv)
        ses.append(result.std_err)

    estimates = np.asarray(pvs, dtype=np.float64)
    rmse = float(estimates.std(ddof=1))
    mean_se = float(np.mean(ses))
    return _RawConfigMeasurement(rmse=rmse, mean_formula_se=mean_se, repetitions=repetitions)


@dataclass(frozen=True)
class ConfigResult:
    """One (cell, config)'s measured result, ready for the report."""

    cell_label: str
    config_id: int
    config_label: str
    rmse: float
    mean_formula_se: float
    calibration_factor: float
    ratio_to_denominator: float
    ratio_rel_precision: float
    repetitions: int


def run_cell(
    cell: Cell,
    *,
    repetitions: int,
    replicates: int,
    paths_per_replicate: int,
    base_seed: int,
    max_batch_bytes: int,
    n_steps: int,
) -> list[ConfigResult]:
    """Measure every applicable configuration at `cell` and return one `ConfigResult` per
    configuration, ratios computed against configuration 1 (pseudo, no antithetics) at this
    SAME cell."""
    configs = _configs_for(cell)
    raw = {
        c.config_id: _measure_config(
            cell,
            c,
            repetitions=repetitions,
            replicates=replicates,
            paths_per_replicate=paths_per_replicate,
            base_seed=base_seed,
            max_batch_bytes=max_batch_bytes,
            n_steps=n_steps,
        )
        for c in configs
    }
    denom_rmse = raw[1].rmse
    rel_precision = 1.0 / math.sqrt(2.0 * (repetitions - 1))

    results: list[ConfigResult] = []
    for c in configs:
        m = raw[c.config_id]
        ratio = denom_rmse / m.rmse if m.rmse > 0.0 else math.inf
        calibration = m.mean_formula_se / m.rmse if m.rmse > 0.0 else math.inf
        results.append(
            ConfigResult(
                cell_label=cell.label,
                config_id=c.config_id,
                config_label=c.label,
                rmse=m.rmse,
                mean_formula_se=m.mean_formula_se,
                calibration_factor=calibration,
                ratio_to_denominator=ratio,
                ratio_rel_precision=rel_precision,
                repetitions=repetitions,
            )
        )
    return results


class InconclusiveVarianceReductionError(ValueError):
    """Raised by `rank_configs` when a cell's grid cannot support a variance-reduction claim:
    either no configuration beats the denominator, or every configuration ties within its own
    measurement precision (module docstring). Carries `rows` so a caller (`run_study`) can still
    preserve the cell's raw measurements as evidence rather than losing them."""

    def __init__(self, message: str, rows: Sequence[ConfigResult]) -> None:
        super().__init__(message)
        self.rows = rows


def rank_configs(rows: Sequence[ConfigResult]) -> ConfigResult:
    """The best (highest `ratio_to_denominator`) non-denominator configuration among `rows` (one
    cell's configurations). Raises `InconclusiveVarianceReductionError` (never falls back) if:

    1. No configuration improves on the denominator: every ratio <= 1.
    2. Every configuration ties: the spread between the best and worst ratio is smaller than
       their combined measurement precision (`ratio_rel_precision`, ~13% at T=32) -- the grid
       cannot distinguish between them.
    """
    candidates = [r for r in rows if r.config_id != 1]
    if not candidates:
        raise ValueError("rank_configs requires at least one non-denominator config in rows")

    if all(c.ratio_to_denominator <= 1.0 for c in candidates):
        raise InconclusiveVarianceReductionError(
            "no configuration improves on the denominator: every ratio <= 1 among "
            f"{[c.config_label for c in candidates]}",
            rows,
        )

    best = max(candidates, key=lambda c: c.ratio_to_denominator)
    worst = min(candidates, key=lambda c: c.ratio_to_denominator)
    if len(candidates) > 1:
        spread = best.ratio_to_denominator - worst.ratio_to_denominator
        tol = (
            best.ratio_to_denominator * best.ratio_rel_precision
            + worst.ratio_to_denominator * worst.ratio_rel_precision
        )
        if spread < tol:
            raise InconclusiveVarianceReductionError(
                "every configuration ties: the spread between the best "
                f"({best.config_label}={best.ratio_to_denominator:.3f}) and worst "
                f"({worst.config_label}={worst.ratio_to_denominator:.3f}) configuration "
                f"({spread:.3f}) is smaller than their combined measurement precision "
                f"({tol:.3f}) -- this cell cannot distinguish between configurations",
                rows,
            )
    return best


@dataclass(frozen=True)
class CellReport:
    cell: Cell
    configs: list[ConfigResult]
    best: ConfigResult | None
    error: str | None
    liveness: AutocallLiveness | None = None


@dataclass(frozen=True)
class StudyResult:
    cell_reports: list[CellReport]
    manifest: RunManifest


def run_study(
    *,
    repetitions: int = DEFAULT_REPETITIONS,
    replicates: int = DEFAULT_REPLICATES,
    paths_per_replicate: int = DEFAULT_PATHS_PER_REPLICATE,
    base_seed: int = DEFAULT_SEED,
    max_batch_bytes: int = DEFAULT_MAX_BATCH_BYTES,
    n_steps: int = DEFAULT_N_STEPS,
    cells: Sequence[Cell] | None = None,
    verbose: bool = True,
) -> StudyResult:
    """Orchestrate the full study: measure every cell's configurations, rank each cell (preserving
    an inconclusive cell's raw rows rather than dropping them -- same posture as
    `scheme_convergence.run_study`), and build the `RunManifest`. Does no file I/O; `main()`
    renders and writes. `cells` defaults to the full production grid (`_cells()`); tests pass a
    tiny subset to stay fast."""
    swept_cells = tuple(cells) if cells is not None else _cells()
    cell_reports: list[CellReport] = []
    start = time.perf_counter()
    for done, cell in enumerate(swept_cells, start=1):
        rows = run_cell(
            cell,
            repetitions=repetitions,
            replicates=replicates,
            paths_per_replicate=paths_per_replicate,
            base_seed=base_seed,
            max_batch_bytes=max_batch_bytes,
            n_steps=n_steps,
        )
        try:
            best: ConfigResult | None = rank_configs(rows)
            error: str | None = None
        except InconclusiveVarianceReductionError as exc:
            best = None
            error = str(exc)

        liveness: AutocallLiveness | None = None
        if cell.product == "autocallable":
            liveness_n_paths = replicates * paths_per_replicate
            liveness_seed = _sub_seed(base_seed, cell.label, "liveness")
            liveness = _measure_autocall_liveness(
                cell.underlying,
                _PARAMS[cell.underlying],
                n_steps=n_steps,
                n_paths=liveness_n_paths,
                seed=liveness_seed,
            )

        cell_reports.append(
            CellReport(cell=cell, configs=rows, best=best, error=error, liveness=liveness)
        )
        if verbose:
            elapsed = time.perf_counter() - start
            print(
                f"[{done}/{len(swept_cells)}] {cell.label} done ({elapsed:.1f}s elapsed)",
                flush=True,
            )

    params_payload = {name: params.model_dump() for name, params in _PARAMS.items()}
    manifest = RunManifest(
        run_id=f"{time.strftime('%Y-%m-%dT%H-%M-%SZ', time.gmtime())}-qmc-variance-reduction",
        created_ns=time.time_ns(),
        git_sha=git_sha(),
        model_id="heston-qmc-variance-reduction-v1",
        params_hash=params_hash(params_payload),
        seed=base_seed,
        n_paths=paths_per_replicate,
        # Reflects configuration 4 (RQMC+bridge) under QE only -- the manifest schema has one
        # `[engine]` table; this study sweeps six configurations per cell (see render_report's
        # own NOTE line, mirroring scheme_convergence's identical manifest-scope caveat).
        engine=EngineSettings(
            scheme="qe",
            n_steps=n_steps,
            antithetic=False,
            sampler="sobol",
            n_replicates=replicates,
            bridge_streams=("spot",),
        ),
        params=params_payload,
    )
    return StudyResult(cell_reports=cell_reports, manifest=manifest)


_DISCLAIMER = (
    "> **Illustrative and UNCALIBRATED parameters (ADR-006 Amendment 4 s1 / Amendment 5).** The "
    "Heston parameter sets swept in this study are demo/development values, not calibrated "
    "market data, restricted to AAPL and MSFT (Amendment 5) -- SPX's Feller-0.231 regime is "
    "P2.M2's warrant-gate trigger and is not swept here."
)


def render_report(
    cell_reports: Sequence[CellReport], manifest: RunManifest, *, repetitions: int
) -> str:
    """Render the study's markdown artifact (written to
    `docs/studies/p2m1-qmc-variance-reduction.md` by `main()`)."""
    rel_precision = 1.0 / math.sqrt(2.0 * (repetitions - 1))
    n_inconclusive = sum(1 for cr in cell_reports if cr.error is not None)
    if n_inconclusive == 0:
        headline = (
            "**Every swept cell found a configuration that measurably beats the pseudo-random "
            "denominator.** See Variance reduction achieved for the per-cell winners."
        )
    else:
        headline = (
            f"**{n_inconclusive}/{len(cell_reports)} cell(s) INCONCLUSIVE** (see Cell results "
            "for the raw evidence each preserved) -- `rank_configs` raised rather than falling "
            "back to a wall-time or best-effort pick."
        )

    lines: list[str] = [
        "# P2.M1 Slice 3 -- QMC Variance-Reduction Study",
        "",
        _DISCLAIMER,
        "",
        headline,
        "",
        "## Provenance",
        "",
        f"- base seed: `{manifest.seed}`",
        f"- paths per replicate: `{manifest.n_paths}`",
        f"- git_sha: `{manifest.git_sha}`",
        f"- params_hash: `{manifest.params_hash}`",
        f"- run_id: `{manifest.run_id}`",
        "- NOTE: `manifest.engine` reflects configuration 4 (RQMC+bridge, QE) only -- the "
        "manifest schema has one `[engine]` table; this study sweeps six configurations per "
        "cell (see Configurations compared / Cell results below).",
        "",
        "## Measurement method",
        "",
        "Formula SEs from `rqmc_estimate` are optimistic by a factor that grows with both "
        "replicate count and net size (heavy-tailed replicate-mean distribution); comparing "
        "them directly to pseudo-random formula SEs would inflate every RQMC ratio. Instead "
        "each (cell, configuration) runs `T` independent repetitions at the identical total "
        "path budget `B`, and `rmse = std(repetition pvs, ddof=1)` -- free of that bias -- is "
        "the error measure this study actually ranks on. The headline ratio is "
        "`rmse_denominator / rmse_config`; `mean_formula_se / rmse` (SE calibration) reports "
        "the bias actually measured on this run's own configuration.",
        "",
        "## Configurations compared",
        "",
        "| # | label | sampler | scope |",
        "|---|---|---|---|",
    ]
    for c in _CONFIGS:
        scope = "barrier cell only" if c.barrier_only else "all cells"
        lines.append(f"| {c.config_id} | {c.label} | {c.sampler} | {scope} |")
    lines.append("")

    lines += [
        "## Variance reduction achieved",
        "",
        f"Headline ratio `rmse_denominator / rmse_config` per cell/config, at T={repetitions} "
        f"repetitions (relative precision +/-{rel_precision * 100:.0f}%, `1/sqrt(2*(T-1))`). "
        "The autocallable is the highest-dimensional, most discontinuous payoff swept here "
        "(early redemption truncates the path, unlike the barrier's smoother survival weight) "
        "-- RQMC's advantage is EXPECTED to be smaller on that cell than on vanilla, and that is "
        "reported as a finding, not adjusted toward a larger number.",
        "",
        "| cell | config | ratio | best? |",
        "|---|---|---|---|",
    ]
    for cr in cell_reports:
        for cfg in cr.configs:
            marker = "<- BEST" if cr.best is not None and cfg.config_id == cr.best.config_id else ""
            lines.append(
                f"| {cr.cell.label} | {cfg.config_label} | "
                f"{cfg.ratio_to_denominator:.2f}x +/- {cfg.ratio_rel_precision * 100:.0f}% | "
                f"{marker} |"
            )
        if cr.error is not None:
            lines.append(f"| {cr.cell.label} | **INCONCLUSIVE** | {cr.error} | |")
    lines.append("")

    lines += [
        "## Bridge contribution",
        "",
        "`rmse(config 3) / rmse(config 4)`: isolates the Brownian bridge's own contribution to "
        "RQMC's variance reduction (both configurations are RQMC, no control variate).",
        "",
        "| cell | ratio |",
        "|---|---|",
    ]
    for cr in cell_reports:
        by_id = {c.config_id: c for c in cr.configs}
        if 3 in by_id and 4 in by_id and by_id[4].rmse > 0.0:
            ratio = by_id[3].rmse / by_id[4].rmse
            lines.append(f"| {cr.cell.label} | {ratio:.2f}x +/- {rel_precision * 100:.0f}% |")
    lines.append("")

    lines += [
        "## Control-variate contribution",
        "",
        "Barrier cells only. `rmse(4)/rmse(5)` isolates the control variate's effect UNDER "
        "RQMC; `rmse(2)/rmse(6)` isolates it WITHOUT RQMC (pseudo-random antithetic baseline).",
        "",
        "| cell | cv under RQMC (4 vs 5) | cv without RQMC (2 vs 6) |",
        "|---|---|---|",
    ]
    for cr in cell_reports:
        if cr.cell.product != "barrier":
            continue
        by_id = {c.config_id: c for c in cr.configs}
        r45 = by_id[4].rmse / by_id[5].rmse if by_id[5].rmse > 0.0 else math.inf
        r26 = by_id[2].rmse / by_id[6].rmse if by_id[6].rmse > 0.0 else math.inf
        lines.append(
            f"| {cr.cell.label} | {r45:.2f}x +/- {rel_precision * 100:.0f}% | "
            f"{r26:.2f}x +/- {rel_precision * 100:.0f}% |"
        )
    lines.append("")

    lines += [
        "## SE calibration",
        "",
        "`mean_formula_se / rmse` per (cell, config) -- the calibration factor measured on this "
        "run's own configuration, replacing the borrowed spike table in `rqmc_estimate`'s "
        "docstring. 1.0 means the formula SE is honest; below 1.0 means it is optimistic.",
        "",
        "| cell | config | calibration factor |",
        "|---|---|---|",
    ]
    for cr in cell_reports:
        for cfg in cr.configs:
            lines.append(f"| {cr.cell.label} | {cfg.config_label} | {cfg.calibration_factor:.3f} |")
    lines.append("")

    lines += [
        "## Peak memory",
        "",
        f"`_PEAK_ARRAY_RATIO = {_PEAK_ARRAY_RATIO}` (tracemalloc-measured, see "
        "`exo/tests/test_qmc_memory.py` and this module's own comment on the constant): peak "
        "bytes for one `price_rqmc`/`simulate()` call is bounded by `_PEAK_ARRAY_RATIO * "
        "n_paths * (n_steps + 1) * 8`. This study refuses to run any (cell, configuration) "
        "whose estimated peak exceeds `--max-batch-bytes` rather than batching within a "
        "repetition -- at this study's defaults, estimated peak per call stays under 300 MB, "
        "far below the 800 MB default ceiling.",
        "",
        "## Cell results",
        "",
        "Raw per-(cell, config) measurements: `rmse` is the honest empirical spread over `T` "
        "repetitions; `mean_formula_se` is the mean of each repetition's own reported standard "
        "error.",
        "",
        "| cell | config | rmse | mean_formula_se | ratio | repetitions |",
        "|---|---|---|---|---|---|",
    ]
    for cr in cell_reports:
        if cr.liveness is not None:
            lv = cr.liveness
            obs_str = ", ".join(f"t={t:g}" for t in lv.observations)
            redeem_str = ", ".join(f"{x:.3f}" for x in lv.redeem_fraction)
            coupon_str = ", ".join(f"{x:.3f}" for x in lv.coupon_fraction)
            lines.append(
                f"_{cr.cell.label} liveness check (product must be genuinely live, not "
                f"G3b's degenerate identity fixture -- see `_autocallable`'s docstring): "
                f"observations = [{obs_str}]; redeem-at-obs fraction = [{redeem_str}]; "
                f"coupon-at-obs fraction = [{coupon_str}]; never-redeemed fraction = "
                f"{lv.never_redeemed_fraction:.3f}_",
            )
        for cfg in cr.configs:
            lines.append(
                f"| {cr.cell.label} | {cfg.config_label} | {cfg.rmse:.5f} | "
                f"{cfg.mean_formula_se:.5f} | {cfg.ratio_to_denominator:.3f} | "
                f"{cfg.repetitions} |"
            )
    lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> None:
    """`python -m exo.studies.qmc_variance_reduction`: run the full production sweep and write
    `docs/studies/p2m1-qmc-variance-reduction.md` plus a `RunManifest`. Not exercised by CI; the
    fast smoke test (`exo/tests/test_qmc_variance_reduction_smoke.py`) calls `run_study` directly
    with tiny overrides instead."""
    parser = argparse.ArgumentParser(
        prog="python -m exo.studies.qmc_variance_reduction",
        description="P2.M1 slice 3 QMC variance-reduction study.",
    )
    parser.add_argument("--repetitions", type=int, default=DEFAULT_REPETITIONS, help="T")
    parser.add_argument("--replicates", type=int, default=DEFAULT_REPLICATES, help="R")
    parser.add_argument(
        "--paths-per-replicate", type=int, default=DEFAULT_PATHS_PER_REPLICATE, help="n"
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="base seed")
    parser.add_argument("--out", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--manifest-dir", type=Path, default=DEFAULT_MANIFEST_DIR)
    parser.add_argument("--max-batch-bytes", type=int, default=DEFAULT_MAX_BATCH_BYTES)
    args = parser.parse_args(argv)

    result = run_study(
        repetitions=args.repetitions,
        replicates=args.replicates,
        paths_per_replicate=args.paths_per_replicate,
        base_seed=args.seed,
        max_batch_bytes=args.max_batch_bytes,
    )
    report = render_report(result.cell_reports, result.manifest, repetitions=args.repetitions)

    out_path: Path = args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report)

    manifest_dir: Path = args.manifest_dir
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / f"{result.manifest.run_id}.toml"
    result.manifest.write(manifest_path)

    print(f"wrote {out_path}")
    print(f"wrote {manifest_path}")

    if any(cr.error is not None for cr in result.cell_reports):
        print(
            "one or more cells were INCONCLUSIVE -- see the written report for the raw "
            "evidence each preserved.",
            file=sys.stderr,
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
