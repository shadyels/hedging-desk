"""Deterministic unit tests for `exo.models.lsm` and `exo.products.pricer.price_american` on
hand-built `PathBundle`s, plus the `LSM_PEAK_ARRAY_RATIO` measurement.

`forced_bundle` (see `_equity_helpers.py`) puts the given spot paths on a uniform grid over
`[0, expiry]` with a constant variance. These tests exist because the statistical gates cannot see
some defects: G-W3 moves only ~-0.02 when continuation values are left undiscounted, and G-W2
holds whatever the regression returns.
"""

from __future__ import annotations

import math
import tracemalloc

import numpy as np
import pytest
from _equity_helpers import forced_bundle
from numpy.typing import NDArray

from exo.models.heston import simulate
from exo.models.lsm import (
    ExercisePolicy,
    apply_exercise_policy,
    fit_exercise_policy,
    quadratic_sv_basis,
)
from exo.models.memory import LSM_PEAK_ARRAY_RATIO
from exo.models.params import EngineConfig, HestonParams
from exo.models.rng import PseudoRandomSource, SobolRandomSource
from exo.products.pricer import LowerBoundPriceResult, price_american
from exo.products.warrant import Warrant

_STRIKE = 100.0


def _put(s: NDArray[np.float64]) -> NDArray[np.float64]:
    out: NDArray[np.float64] = np.maximum(_STRIKE - s, 0.0)
    return out


def _call(s: NDArray[np.float64]) -> NDArray[np.float64]:
    out: NDArray[np.float64] = np.maximum(s - _STRIKE, 0.0)
    return out


def test_continuation_is_discounted_so_intrinsic_between_the_two_exercises() -> None:
    """Pilot: 100 paths, S_T = 60 (put payoff 40) on every path, S at the single early-exercise
    date t=0.5 in [64, 66] (intrinsic 34..36). r = 0.5. The CORRECT continuation at t=0.5 is the
    DISCOUNTED payoff 40 e^{-0.25} = 31.15 < intrinsic -> exercise; a regression on the
    UNDISCOUNTED payoff sees 40 > intrinsic -> holds. G-W3 cannot see this bug (undiscounted
    continuation moves it only ~-0.02), so it is pinned here."""
    r = 0.5
    s_mid = np.linspace(64.0, 66.0, 100)
    paths = [[100.0, float(s), 60.0] for s in s_mid]
    bundle = forced_bundle(paths)  # t = 0, 0.5, 1.0

    discounted_continuation = 40.0 * math.exp(-r * 0.5)
    g = _put(s_mid)
    assert discounted_continuation < g.min() and g.max() < 40.0  # the discriminating inequality

    policy = fit_exercise_policy(
        bundle,
        _put,
        exercise_idx=np.array([1], dtype=np.intp),
        terminal_idx=2,
        r=r,
        q=0.0,
    )
    coef = policy.coefficients[0]
    assert coef is not None
    fitted = quadratic_sv_basis(s_mid / 100.0, bundle.v[:, 1]) @ coef
    np.testing.assert_allclose(fitted, discounted_continuation, atol=1e-9)

    outcome = apply_exercise_policy(bundle, _put, policy, r=r, q=0.0)
    np.testing.assert_array_equal(outcome.exercise_step, 1)
    np.testing.assert_allclose(outcome.discounted, math.exp(-r * 0.5) * g, atol=1e-12)


def _const_basis(x: NDArray[np.float64], v: NDArray[np.float64]) -> NDArray[np.float64]:
    return np.ones((x.shape[0], 1))


def test_hand_policy_bookkeeping_first_exercise_wins_and_discounts_by_exercise_date() -> None:
    """Constant-continuation policy (c = 20 at t=1, 10 at t=2), put K=100, r=0.05, q=0, grid
    t = 0..3. Paths: P0 exercises at 1; P1 holds at 1 (10 < 20) and exercises at 2; P2 never
    exercises (5 < 10) and is paid at the terminal date; P3 is deep ITM at every date but is
    exercised ONCE, at the first; P4 is OTM throughout."""
    r = 0.05
    bundle = forced_bundle(
        [
            [100.0, 70.0, 60.0, 60.0],
            [100.0, 90.0, 70.0, 80.0],
            [100.0, 90.0, 95.0, 80.0],
            [100.0, 50.0, 40.0, 30.0],
            [100.0, 110.0, 120.0, 130.0],
        ],
        expiry=3.0,
    )
    policy = ExercisePolicy(
        exercise_idx=np.array([1, 2], dtype=np.intp),
        terminal_idx=3,
        coefficients=(np.array([20.0]), np.array([10.0])),
        basis=_const_basis,
        s_scale=100.0,
    )
    outcome = apply_exercise_policy(bundle, _put, policy, r=r, q=0.0)

    np.testing.assert_array_equal(outcome.exercise_step, [1, 2, 3, 1, 3])
    expected = [
        math.exp(-r * 1) * 30.0,
        math.exp(-r * 2) * 30.0,
        math.exp(-r * 3) * 20.0,
        math.exp(-r * 1) * 50.0,
        0.0,
    ]
    np.testing.assert_allclose(outcome.discounted, expected, atol=1e-12)


def test_jensen_floor_blocks_exercise_even_when_the_regression_says_zero() -> None:
    """Zero-dividend call: every pilot path ends OTM, so the regressed continuation is 0 while the
    call is ITM at the early dates. Exercise is still wrong: the Jensen floor
    `e^{-rD} g(S e^{rD}) = S - K e^{-rD}` exceeds the intrinsic `S - K`. Nothing may exercise."""
    r = 0.05
    paths = [[100.0, 120.0 + 0.01 * i, 125.0, 50.0] for i in range(100)]
    bundle = forced_bundle(paths)  # t = 0, 1/3, 2/3, 1
    policy = fit_exercise_policy(
        bundle,
        _call,
        exercise_idx=np.array([1, 2], dtype=np.intp),
        terminal_idx=3,
        r=r,
        q=0.0,
    )
    for j, idx in enumerate((1, 2)):
        coef = policy.coefficients[j]
        assert coef is not None
        fitted = quadratic_sv_basis(bundle.S[:, idx] / 100.0, bundle.v[:, idx]) @ coef
        assert np.all(fitted < _call(bundle.S[:, idx]))  # without the floor these WOULD exercise

    outcome = apply_exercise_policy(bundle, _call, policy, r=r, q=0.0)
    np.testing.assert_array_equal(outcome.exercise_step, 3)
    np.testing.assert_allclose(outcome.discounted, 0.0, atol=1e-12)


def test_too_few_itm_pilot_paths_means_no_regression_and_no_exercise() -> None:
    """30 ITM pilot paths < 10 * 6 basis functions -> coefficients None -> the policy never
    exercises there, even for a deep-ITM valuation path."""
    pilot = forced_bundle([[100.0, 50.0, 50.0]] * 30 + [[100.0, 150.0, 150.0]] * 70)
    policy = fit_exercise_policy(
        pilot, _put, exercise_idx=np.array([1], dtype=np.intp), terminal_idx=2, r=0.05, q=0.0
    )
    assert policy.coefficients == (None,)

    valuation = forced_bundle([[100.0, 10.0, 10.0]] * 4)
    outcome = apply_exercise_policy(valuation, _put, policy, r=0.05, q=0.0)
    np.testing.assert_array_equal(outcome.exercise_step, 2)


def test_rank_deficient_regression_under_a_bs_degenerate_model_is_finite() -> None:
    """v is constant, so the v, v^2 and x*v columns are collinear with the intercept (rank 3 of 6):
    `lstsq` must return the finite min-norm solution rather than raising or producing NaN."""
    params = HestonParams(s0=100.0, r=0.06, q=0.0, v0=0.04, kappa=1.5, theta=0.04, xi=1e-4, rho=0.0)
    engine = EngineConfig(scheme="qe", n_steps=10, n_paths=4_000, expiry=1.0, antithetic=True)
    bundle = simulate(params, engine, PseudoRandomSource(seed=3))
    policy = fit_exercise_policy(
        bundle,
        _put,
        exercise_idx=np.arange(1, 10, dtype=np.intp),
        terminal_idx=10,
        r=0.06,
        q=0.0,
    )
    fitted = [c for c in policy.coefficients if c is not None]
    assert fitted, "no date had enough ITM paths"
    assert all(np.all(np.isfinite(c)) for c in fitted)
    outcome = apply_exercise_policy(bundle, _put, policy, r=0.06, q=0.0)
    assert np.all(np.isfinite(outcome.discounted))


def test_low_discrepancy_bundle_is_refused_by_fit_and_apply() -> None:
    bundle = forced_bundle([[100.0, 90.0, 80.0]] * 100)
    sobol = type(bundle)(
        t=bundle.t,
        S=bundle.S,
        v=bundle.v,
        antithetic=False,
        n_pairs=None,
        qe_fallback_count=None,
        low_discrepancy=True,
    )
    idx = np.array([1], dtype=np.intp)
    with pytest.raises(ValueError, match="low-discrepancy"):
        fit_exercise_policy(sobol, _put, exercise_idx=idx, terminal_idx=2, r=0.05, q=0.0)
    policy = fit_exercise_policy(bundle, _put, exercise_idx=idx, terminal_idx=2, r=0.05, q=0.0)
    with pytest.raises(ValueError, match="low-discrepancy"):
        apply_exercise_policy(sobol, _put, policy, r=0.05, q=0.0)


@pytest.mark.parametrize("idx", [[0, 1], [2, 1], [1, 1], [1, 2]])
def test_malformed_exercise_grid_raises(idx: list[int]) -> None:
    bundle = forced_bundle([[100.0, 90.0, 80.0]] * 100)
    with pytest.raises(ValueError, match="exercise_idx"):
        fit_exercise_policy(
            bundle,
            _put,
            exercise_idx=np.array(idx, dtype=np.intp),
            terminal_idx=2,
            r=0.05,
            q=0.0,
        )


_PARAMS = HestonParams(s0=100.0, r=0.06, q=0.0, v0=0.04, kappa=1.5, theta=0.04, xi=1e-4, rho=0.0)


def _warrant(**overrides: object) -> Warrant:
    kwargs: dict[str, object] = {
        "underlying": "SYN",
        "option_type": "put",
        "strike": 100.0,
        "expiry": 1.0,
        "ratio": 1.0,
        "exercise": "american",
    }
    kwargs.update(overrides)
    return Warrant(**kwargs)  # type: ignore[arg-type]


def _engine(n_paths: int = 2_000, n_steps: int = 10, expiry: float = 1.0) -> EngineConfig:
    return EngineConfig(
        scheme="qe", n_steps=n_steps, n_paths=n_paths, expiry=expiry, antithetic=True
    )


def test_price_american_returns_a_lower_bound_result_with_an_exercise_count() -> None:
    result = price_american(
        _warrant(),
        _PARAMS,
        _engine(),
        PseudoRandomSource(seed=1),
        pilot_rng=PseudoRandomSource(seed=2),
    )
    assert isinstance(result, LowerBoundPriceResult)
    assert 0 < result.n_early_exercised < 2_000
    assert result.price.std_err > 0.0 and result.price.n_paths == 2_000


def test_price_american_refuses_a_european_warrant() -> None:
    with pytest.raises(ValueError, match="american"):
        price_american(
            _warrant(exercise="european"),
            _PARAMS,
            _engine(),
            PseudoRandomSource(seed=1),
            pilot_rng=PseudoRandomSource(seed=2),
        )


def test_price_american_requires_engine_expiry_to_match_exactly() -> None:
    with pytest.raises(ValueError, match="expiry"):
        price_american(
            _warrant(),
            _PARAMS,
            _engine(expiry=2.0),
            PseudoRandomSource(seed=1),
            pilot_rng=PseudoRandomSource(seed=2),
        )


def test_price_american_refuses_an_in_sample_pilot() -> None:
    """Equal seeds replay identical streams: the pilot WOULD BE the valuation sample, an in-sample
    foresight bias that G-W3 cannot detect (measured ~0.5 SE), so the guard is structural."""
    with pytest.raises(ValueError, match="seed"):
        price_american(
            _warrant(),
            _PARAMS,
            _engine(),
            PseudoRandomSource(seed=5),
            pilot_rng=PseudoRandomSource(seed=5),
        )


def _sobol(seed: int) -> SobolRandomSource:
    return SobolRandomSource(
        seed=seed, n_paths=2_048, dims=(("variance", 10), ("spot", 10)), replicate=0
    )


@pytest.mark.parametrize("which", ["rng", "pilot"])
def test_price_american_refuses_low_discrepancy_sources(which: str) -> None:
    pseudo = PseudoRandomSource(seed=1)
    with pytest.raises(NotImplementedError, match="RQMC"):
        price_american(
            _warrant(),
            _PARAMS,
            _engine(n_paths=2_048),
            _sobol(1) if which == "rng" else pseudo,
            pilot_rng=_sobol(2) if which == "pilot" else PseudoRandomSource(seed=2),
        )


def test_lsm_peak_memory_covers_measured_tracemalloc_ratio() -> None:
    """Measures the REAL `price_american` call at 20_000 paths x 50 steps (QE, antithetic), never
    re-derives the ratio by counting arrays. Warm-up call OUTSIDE the tracemalloc window so
    one-time lazy imports are not counted. Measured peak/array (array = one `(n_paths, n_steps +
    1)` float64): 4.982, repeat 4.982, 4.981 at 40_000 paths -- exactly one bundle alive at peak.
    A pilot bundle kept alive through the valuation `simulate()` would read ~7 and fail the
    ceiling: fix the reference lifetime, never raise `LSM_PEAK_ARRAY_RATIO`.

    Asserts `measured <= ceiling` AND `ceiling <= measured * 1.06`.
    """
    n_paths, n_steps = 20_000, 50
    engine = _engine(n_paths=n_paths, n_steps=n_steps)
    warrant = _warrant()
    price_american(
        warrant,
        _PARAMS,
        engine,
        PseudoRandomSource(seed=1),
        pilot_rng=PseudoRandomSource(seed=2),
    )  # warm-up, outside the measured window

    tracemalloc.start()
    try:
        price_american(
            warrant,
            _PARAMS,
            engine,
            PseudoRandomSource(seed=3),
            pilot_rng=PseudoRandomSource(seed=4),
        )
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    measured = peak / (n_paths * (n_steps + 1) * 8)
    ceiling = LSM_PEAK_ARRAY_RATIO
    assert measured <= ceiling, (
        f"measured peak/array {measured:.3f} exceeds the declared ceiling {ceiling} -- is the "
        "pilot bundle still referenced when the valuation simulate() runs?"
    )
    assert ceiling <= measured * 1.06, (
        f"ceiling {ceiling} is more than 6% above the measured {measured:.3f} -- re-measure and "
        "tighten it"
    )
