"""Tests for exo.products.base: CashflowLedger/discount algebra, observation_indices'
alignment guard, antithetic row-order preservation through the ledger, and
barrier_survival's bridge/discrete guards.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from exo.models.estimator import mc_estimate
from exo.models.heston import simulate
from exo.models.params import EngineConfig, HestonParams
from exo.models.rng import PseudoRandomSource
from exo.products.base import (
    CashflowLedger,
    Monitoring,
    ScheduleAlignmentError,
    barrier_survival,
    observation_indices,
)
from exo.products.pricer import discount

_PARAMS = HestonParams(s0=100.0, r=0.02, q=0.01, v0=0.04, kappa=1.5, theta=0.04, xi=0.6, rho=-0.7)


def _bundle(n_steps: int = 10, n_paths: int = 200, expiry: float = 1.0, seed: int = 1) -> object:
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
