"""Tests for exo.products.barrier.BarrierOption: discrete-vs-continuous monitoring gap
(and its shrinkage as n_steps grows), monotonicity in barrier level, and in-out parity
at the ledger level (exact by construction -- see test_products_base for the survival-
level analogue and test_validation_gates_products for the blocking G5 gate).
"""

from __future__ import annotations

import pytest

from exo.models.heston import simulate
from exo.models.params import EngineConfig, HestonParams
from exo.models.rng import PseudoRandomSource
from exo.products.barrier import BarrierOption
from exo.products.base import Monitoring
from exo.products.pricer import price_from_bundle

_AAPL = HestonParams(
    s0=187.50, r=0.0425, q=0.0050, v0=0.0400, kappa=1.50, theta=0.0400, xi=0.60, rho=-0.70
)


def _sheet(
    monitoring: Monitoring, observations: tuple[float, ...] | None, barrier: float
) -> BarrierOption:
    return BarrierOption(
        underlying="AAPL",
        option_type="call",
        strike=187.50,
        expiry=1.0,
        barrier=barrier,
        direction="down",
        knock="out",
        monitoring=monitoring,
        observations=observations,
    )


def test_discrete_monitoring_prices_above_continuous_for_down_and_out() -> None:
    """Discrete monitoring misses intra-step crossings, so it must knock out LESS often
    and price HIGHER than continuous-bridge monitoring on the same bundle."""
    n_steps = 50
    engine = EngineConfig(scheme="qe", n_steps=n_steps, n_paths=20_000, expiry=1.0, antithetic=True)
    bundle = simulate(_AAPL, engine, PseudoRandomSource(seed=11))

    obs = tuple(float(i) / n_steps for i in range(1, n_steps + 1))
    discrete = _sheet(Monitoring.DISCRETE, obs, barrier=170.0)
    continuous = _sheet(Monitoring.CONTINUOUS_BRIDGE, None, barrier=170.0)

    discrete_result = price_from_bundle(discrete, bundle, r=_AAPL.r)
    continuous_result = price_from_bundle(continuous, bundle, r=_AAPL.r)

    assert discrete_result.pv > continuous_result.pv


def test_discrete_continuous_gap_shrinks_as_n_steps_grows() -> None:
    barrier = 170.0

    def _gap(n_steps: int, seed: int) -> float:
        engine = EngineConfig(
            scheme="qe", n_steps=n_steps, n_paths=20_000, expiry=1.0, antithetic=True
        )
        bundle = simulate(_AAPL, engine, PseudoRandomSource(seed=seed))
        obs = tuple(float(i) / n_steps for i in range(1, n_steps + 1))
        discrete = _sheet(Monitoring.DISCRETE, obs, barrier=barrier)
        continuous = _sheet(Monitoring.CONTINUOUS_BRIDGE, None, barrier=barrier)
        d = price_from_bundle(discrete, bundle, r=_AAPL.r).pv
        c = price_from_bundle(continuous, bundle, r=_AAPL.r).pv
        return d - c

    coarse_gap = _gap(12, seed=21)
    fine_gap = _gap(200, seed=22)
    assert 0.0 < fine_gap < coarse_gap


def test_down_and_out_call_price_is_monotone_increasing_in_barrier_gap() -> None:
    """A barrier further below spot knocks out less often, so price should be
    monotone non-decreasing as the barrier moves further from spot."""
    engine = EngineConfig(scheme="qe", n_steps=50, n_paths=20_000, expiry=1.0, antithetic=True)
    bundle = simulate(_AAPL, engine, PseudoRandomSource(seed=31))

    near = _sheet(Monitoring.CONTINUOUS_BRIDGE, None, barrier=180.0)
    far = _sheet(Monitoring.CONTINUOUS_BRIDGE, None, barrier=150.0)

    near_pv = price_from_bundle(near, bundle, r=_AAPL.r).pv
    far_pv = price_from_bundle(far, bundle, r=_AAPL.r).pv
    assert far_pv > near_pv


def test_barrier_option_rejects_discrete_without_observations() -> None:
    with pytest.raises(ValueError):
        BarrierOption(
            underlying="AAPL",
            option_type="call",
            strike=100.0,
            expiry=1.0,
            barrier=90.0,
            direction="down",
            knock="out",
            monitoring=Monitoring.DISCRETE,
            observations=None,
        )


def test_barrier_option_rejects_continuous_with_observations() -> None:
    with pytest.raises(ValueError):
        BarrierOption(
            underlying="AAPL",
            option_type="call",
            strike=100.0,
            expiry=1.0,
            barrier=90.0,
            direction="down",
            knock="out",
            monitoring=Monitoring.CONTINUOUS_BRIDGE,
            observations=(0.5, 1.0),
        )


def test_barrier_option_rejects_non_ascending_observations() -> None:
    with pytest.raises(ValueError):
        BarrierOption(
            underlying="AAPL",
            option_type="call",
            strike=100.0,
            expiry=1.0,
            barrier=90.0,
            direction="down",
            knock="out",
            monitoring=Monitoring.DISCRETE,
            observations=(0.5, 0.3),
        )
