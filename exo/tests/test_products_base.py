"""Tests for exo.products.base: CashflowLedger/discount algebra, observation_indices'
alignment guard, antithetic row-order preservation through the ledger, and
barrier_survival's bridge/discrete guards.
"""

from __future__ import annotations

import math
import tracemalloc

import numpy as np
import pytest

from exo.models.estimator import mc_estimate
from exo.models.heston import PathBundle, simulate
from exo.models.params import EngineConfig, HestonParams
from exo.models.rng import PseudoRandomSource
from exo.products import base
from exo.products.base import (
    CashflowLedger,
    Monitoring,
    ScheduleAlignmentError,
    barrier_survival,
    observation_indices,
    validate_observation_schedule,
)
from exo.products.pricer import discount

_PARAMS = HestonParams(s0=100.0, r=0.02, q=0.01, v0=0.04, kappa=1.5, theta=0.04, xi=0.6, rho=-0.7)


def _bundle(
    n_steps: int = 10, n_paths: int = 200, expiry: float = 1.0, seed: int = 1
) -> PathBundle:
    engine = EngineConfig(
        scheme="qe", n_steps=n_steps, n_paths=n_paths, expiry=expiry, antithetic=True
    )
    return simulate(_PARAMS, engine, PseudoRandomSource(seed=seed))


def test_discount_applies_exp_minus_rt_per_cashflow_date() -> None:
    ledger = CashflowLedger(
        t=np.array([0.5, 1.0]),
        amounts=np.array([[10.0, 0.0], [0.0, 20.0], [5.0, 5.0]]),
    )
    r = 0.03
    result = discount(ledger, r=r)
    expected = np.array(
        [
            10.0 * math.exp(-r * 0.5),
            20.0 * math.exp(-r * 1.0),
            5.0 * math.exp(-r * 0.5) + 5.0 * math.exp(-r * 1.0),
        ]
    )
    np.testing.assert_allclose(result, expected, rtol=1e-12, atol=1e-12)


def test_observation_indices_maps_onto_grid_points() -> None:
    bundle = _bundle(n_steps=10, expiry=1.0)
    # grid points are at 0.0, 0.1, 0.2, ..., 1.0
    idx = observation_indices(np.array([0.3, 0.7, 1.0]), bundle)
    np.testing.assert_array_equal(idx, np.array([3, 7, 10]))


def test_observation_indices_raises_on_misaligned_observation() -> None:
    bundle = _bundle(n_steps=10, expiry=1.0)
    with pytest.raises(ScheduleAlignmentError):
        observation_indices(np.array([0.35]), bundle)


def test_observation_indices_raises_on_t_equal_zero() -> None:
    bundle = _bundle(n_steps=10, expiry=1.0)
    with pytest.raises(ScheduleAlignmentError):
        observation_indices(np.array([0.0]), bundle)


def test_observation_indices_raises_on_nan_observation() -> None:
    """HIGH-3 (security review, fix round 1): before the fix, `nearest_dist > tol` is False
    for NaN under IEEE-754, so a NaN observation was silently mapped to an arbitrary grid
    index instead of being rejected -- defeating the exact guard `ScheduleAlignmentError`
    exists to enforce. Verified on the pre-fix code: this returned index [4] with no raise."""
    bundle = _bundle(n_steps=10, expiry=1.0)
    with pytest.raises(ScheduleAlignmentError):
        observation_indices(np.array([float("nan")]), bundle)


def test_observation_indices_raises_on_inf_observation() -> None:
    bundle = _bundle(n_steps=10, expiry=1.0)
    with pytest.raises(ScheduleAlignmentError):
        observation_indices(np.array([float("inf")]), bundle)


@pytest.mark.parametrize("require_terminal", [False, True])
def test_validate_observation_schedule_rejects_nan(require_terminal: bool) -> None:
    with pytest.raises(ValueError):
        validate_observation_schedule((0.5, float("nan")), 1.0, require_terminal=require_terminal)


def test_validate_observation_schedule_require_terminal_uses_grid_tolerance() -> None:
    """LOW-1 (code review, fix round 1): exact float equality bites on accumulated
    schedules -- `sum(1/12 for _ in range(24))` is `1.9999999999999991 != 2.0` -- so
    `require_terminal` must use the same `1e-9 * expiry` tolerance `observation_indices`
    already defines, not exact equality."""
    expiry = 2.0
    obs_list = []
    acc = 0.0
    for _ in range(24):
        acc += 1.0 / 12.0
        obs_list.append(acc)
    observations = tuple(obs_list)
    assert observations[-1] != expiry  # sanity: really the float-drift case (1.9999999999999991)
    validate_observation_schedule(observations, expiry, require_terminal=True)


def test_validate_observation_schedule_require_terminal_rejects_real_mismatch() -> None:
    with pytest.raises(ValueError):
        validate_observation_schedule((0.5, 0.9), 1.0, require_terminal=True)


def test_antithetic_row_order_preserved_through_ledger_and_estimator() -> None:
    """A payoff whose per-path amount is exactly its path index must produce the SAME
    pair means through discount+mc_estimate as computing them by hand from the raw
    index array -- if the ledger silently reordered rows, this would diverge."""
    bundle = _bundle(n_steps=5, n_paths=40, expiry=1.0)
    n_paths = bundle.S.shape[0]
    amounts = np.arange(n_paths, dtype=np.float64).reshape(-1, 1)
    ledger = CashflowLedger(t=np.array([bundle.t[-1]]), amounts=amounts)

    disc = discount(ledger, r=0.0)  # r=0 so discounting is a no-op, isolating row order
    np.testing.assert_allclose(disc, np.arange(n_paths, dtype=np.float64))

    result = mc_estimate(bundle, disc)

    n_pairs = bundle.n_pairs
    assert n_pairs is not None
    hand_pair_means = 0.5 * (disc[:n_pairs] + disc[n_pairs:])
    assert result.pv == pytest.approx(float(hand_pair_means.mean()))
    assert result.std_err == pytest.approx(float(hand_pair_means.std(ddof=1) / math.sqrt(n_pairs)))


def test_barrier_survival_is_within_unit_interval() -> None:
    bundle = _bundle(n_steps=20, n_paths=500, expiry=1.0)
    obs_idx = np.arange(bundle.t.shape[0])
    survival = barrier_survival(bundle, 90.0, "down", Monitoring.CONTINUOUS_BRIDGE, obs_idx)
    assert survival.shape == (bundle.S.shape[0],)
    assert np.all(survival >= 0.0)
    assert np.all(survival <= 1.0)


def test_barrier_survival_discrete_is_hard_indicator() -> None:
    bundle = _bundle(n_steps=20, n_paths=500, expiry=1.0)
    obs_idx = observation_indices(np.array([0.5, 1.0]), bundle)
    survival = barrier_survival(bundle, 90.0, "down", Monitoring.DISCRETE, obs_idx)
    assert set(np.unique(survival).tolist()) <= {0.0, 1.0}
    breached = np.any(bundle.S[:, obs_idx] <= 90.0, axis=1)
    np.testing.assert_array_equal(survival, np.where(breached, 0.0, 1.0))


def test_barrier_survival_allows_a_contiguous_prefix_for_continuous_bridge() -> None:
    """A payoff whose expiry is shorter than the bundle's own horizon (HIGH-1, code review,
    fix round 1 -- e.g. price_from_bundle sharing one longer bundle across products) passes a
    strict PREFIX of the grid, not the whole thing. That must be accepted."""
    bundle = _bundle(n_steps=20, n_paths=50, expiry=1.0)
    prefix_idx = np.arange(bundle.t.shape[0] - 1)  # [0, 1, ..., n_steps-1]: contiguous from 0
    survival = barrier_survival(bundle, 90.0, "down", Monitoring.CONTINUOUS_BRIDGE, prefix_idx)
    assert survival.shape == (bundle.S.shape[0],)


def test_barrier_survival_rejects_a_gap_in_the_grid_for_continuous_bridge() -> None:
    """LOW-2 (code review, fix round 1): CONTINUOUS_BRIDGE's `np.errstate(over="ignore")`
    argument is airtight only because `obs_idx` is a CONTIGUOUS, gap-free run from 0; nothing
    enforced that precondition before this fix. A gap in the middle would silently apply the
    left-point variance approximation across multiple simulation steps at once instead of one."""
    bundle = _bundle(n_steps=20, n_paths=50, expiry=1.0)
    gapped_idx = np.array([0, 1, 2, 4, 5, 6])  # skips index 3: a genuine gap, not a prefix
    with pytest.raises(ValueError):
        barrier_survival(bundle, 90.0, "down", Monitoring.CONTINUOUS_BRIDGE, gapped_idx)


def test_barrier_survival_rejects_invalid_direction() -> None:
    """MEDIUM-6 (security review, fix round 1): `direction` is consumed through a two-way
    `if x == "down" else ...` branch with no explicit membership check, so a case typo would
    otherwise be silently treated as the OTHER direction."""
    bundle = _bundle(n_steps=10, n_paths=50, expiry=1.0)
    obs_idx = np.arange(bundle.t.shape[0])
    with pytest.raises(ValueError):
        barrier_survival(bundle, 90.0, "Down", Monitoring.CONTINUOUS_BRIDGE, obs_idx)  # type: ignore[arg-type]


@pytest.mark.parametrize("monitoring", [Monitoring.CONTINUOUS_BRIDGE, Monitoring.DISCRETE])
def test_barrier_survival_rejects_non_finite_level(monitoring: Monitoring) -> None:
    """R2-2 (security review, fix round 2): before this fix, a NaN `level` under DISCRETE made
    `obs_s <= level` False everywhere, so survival came back a silent 1.0 (never-breached) with
    NO signal at all -- a wrong answer, not just a propagated NaN."""
    bundle = _bundle(n_steps=10, n_paths=50, expiry=1.0)
    obs_idx = np.arange(bundle.t.shape[0])
    with pytest.raises(ValueError):
        barrier_survival(bundle, float("nan"), "down", monitoring, obs_idx)


def test_cashflow_ledger_rejects_mismatched_shapes() -> None:
    """LOW-5 (code review, fix round 1): `amounts` must be `(n_paths, n_obs)` with
    `amounts.shape[1] == t.shape[0]` -- a payoff returning `(n_obs, n_paths)` must fail loud
    at construction, not surface as an opaque matmul error downstream in `discount()`."""
    with pytest.raises(ValueError):
        CashflowLedger(t=np.array([0.5, 1.0]), amounts=np.zeros((4, 3)))


def test_cashflow_ledger_rejects_non_ascending_t() -> None:
    with pytest.raises(ValueError):
        CashflowLedger(t=np.array([1.0, 0.5]), amounts=np.zeros((4, 2)))


def test_cashflow_ledger_rejects_1d_amounts() -> None:
    with pytest.raises(ValueError):
        CashflowLedger(t=np.array([1.0]), amounts=np.zeros(4))


def test_barrier_survival_clamps_negative_variance_state() -> None:
    """euler-ft may store negative variance; barrier_survival must clamp v_i at 0
    before using it (not raise, not propagate NaN through the bridge exponent)."""
    engine = EngineConfig(
        scheme="euler-ft", n_steps=20, n_paths=20_000, expiry=1.0, antithetic=True
    )
    bundle = simulate(_PARAMS, engine, PseudoRandomSource(seed=7))
    assert np.any(bundle.v < 0.0)  # sanity: this fixture actually exercises the clamp
    obs_idx = np.arange(bundle.t.shape[0])
    survival = barrier_survival(bundle, 90.0, "down", Monitoring.CONTINUOUS_BRIDGE, obs_idx)
    assert np.all(np.isfinite(survival))
    assert np.all(survival >= 0.0)
    assert np.all(survival <= 1.0)


def test_bridge_peak_memory_covers_measured_tracemalloc_ratio() -> None:
    """MEDIUM-7 (security review, fix round 1): the reviewer estimated ~8 live
    `(n_paths, n_steps+1)`-sized arrays in the CONTINUOUS_BRIDGE branch from reading the code,
    and explicitly flagged that as an order-of-magnitude estimate, NOT a measurement --
    recommending `tracemalloc` before trusting it, since this repo's own `heston.py` memory
    ponytail was hand-counted wrong TWICE (810 MB, then 1.62 GB) before a measurement settled
    it at ~2.02 GB, always understating. This measures the REAL `barrier_survival` call
    instead, at two sizes, and asserts the ratio matches what was actually observed --
    ~10.2 at both n_paths=20_000/n_steps=100 and n_paths=40_000/n_steps=200 when this test was
    written (see `base.py`'s own `P2-7` ponytail marker for the number this backs).

    R2-1 (fix round 2): asserts against `base._BRIDGE_PEAK_ARRAY_RATIO`, the SAME module-level
    constant the `P2-7` marker's declared ceiling names -- not a test-local literal -- mirroring
    how `test_bytes_per_path_step_covers_measured_peak_with_tracemalloc` reads
    `scheme_convergence._BYTES_PER_PATH_STEP`, so the declared number and this guard cannot
    drift apart the way a comment-only figure could.
    """
    params = HestonParams(
        s0=100.0, r=0.02, q=0.01, v0=0.04, kappa=1.5, theta=0.04, xi=0.6, rho=-0.7
    )
    tolerance = 0.5

    for n_paths, n_steps in [(20_000, 100), (40_000, 200)]:
        engine = EngineConfig(
            scheme="qe", n_steps=n_steps, n_paths=n_paths, expiry=1.0, antithetic=True
        )
        bundle = simulate(params, engine, PseudoRandomSource(seed=1, antithetic=True))
        obs_idx = np.arange(bundle.t.shape[0])
        bytes_per_array = n_paths * (n_steps + 1) * 8

        tracemalloc.start()
        try:
            barrier_survival(bundle, 90.0, "down", Monitoring.CONTINUOUS_BRIDGE, obs_idx)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        measured_ratio = peak / bytes_per_array
        assert measured_ratio - tolerance <= base._BRIDGE_PEAK_ARRAY_RATIO, (
            f"n_paths={n_paths} n_steps={n_steps}: measured peak/array ratio "
            f"{measured_ratio:.3f} exceeds the declared ceiling "
            f"({base._BRIDGE_PEAK_ARRAY_RATIO}) by more than the noise tolerance "
            f"({tolerance}) -- P2-7's marker no longer covers what actually runs"
        )
        assert measured_ratio > 5.0, (
            f"n_paths={n_paths} n_steps={n_steps}: measured ratio {measured_ratio:.3f} is "
            "implausibly small -- this measurement itself looks broken, not the marker"
        )
