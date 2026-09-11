"""Tests for exo.products.barrier.BarrierOption: discrete-vs-continuous monitoring gap
(and its shrinkage as n_steps grows), monotonicity in barrier level, and in-out parity
at the ledger level (exact by construction -- see test_products_base for the survival-
level analogue and test_validation_gates_products for the blocking G5 gate).
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("strike", float("nan")),
        ("barrier", float("nan")),
        ("expiry", float("nan")),
        ("strike", float("inf")),
        ("barrier", float("inf")),
    ],
)
def test_barrier_option_rejects_non_finite_scalar_fields(field: str, value: float) -> None:
    """HIGH-3 (security review, fix round 1): before the fix, e.g. `barrier=nan` passed
    __post_init__'s `<= 0.0` check (False for NaN) with no exception anywhere."""
    kwargs: dict[str, object] = dict(
        underlying="AAPL",
        option_type="call",
        strike=100.0,
        expiry=1.0,
        barrier=90.0,
        direction="down",
        knock="out",
        monitoring=Monitoring.CONTINUOUS_BRIDGE,
        observations=None,
    )
    kwargs[field] = value
    with pytest.raises(ValueError):
        BarrierOption(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["option_type", "direction", "knock"])
def test_barrier_option_rejects_case_typo_in_categorical_fields(field: str) -> None:
    """MEDIUM-6 (security review, fix round 1): before the fix, `option_type="Call"` (a case
    typo) was silently accepted by __post_init__ and then priced as the OTHER branch (a put),
    since every categorical field is consumed through a two-way `if x == ... else` branch."""
    kwargs: dict[str, object] = dict(
        underlying="AAPL",
        option_type="call",
        strike=100.0,
        expiry=1.0,
        barrier=90.0,
        direction="down",
        knock="out",
        monitoring=Monitoring.CONTINUOUS_BRIDGE,
        observations=None,
    )
    kwargs[field] = str(kwargs[field]).capitalize()
    with pytest.raises(ValueError):
        BarrierOption(**kwargs)  # type: ignore[arg-type]


def test_price_from_bundle_rejects_bundle_horizon_mismatch() -> None:
    """HIGH-1 (code review, fix round 1): `price_from_bundle` had no guard against a bundle
    simulated to a different horizon than the payoff -- measured, pre-fix, at a 13.5 SE silent
    mispricing on a 2-year bundle against a 1-year down-and-out call."""
    engine = EngineConfig(scheme="qe", n_steps=20, n_paths=4, expiry=2.0, antithetic=False)
    bundle = simulate(_AAPL, engine, PseudoRandomSource(seed=1, antithetic=False))
    opt = _sheet(Monitoring.CONTINUOUS_BRIDGE, None, barrier=150.0)  # opt.expiry == 1.0

    with pytest.raises(ValueError):
        price_from_bundle(opt, bundle, r=_AAPL.r)


def test_cashflows_ignores_bundle_steps_beyond_its_own_expiry() -> None:
    """HIGH-1 (code review, fix round 1): before the fix, CONTINUOUS_BRIDGE monitored
    `np.arange(bundle.t.shape[0])` (the WHOLE bundle horizon) and read the terminal spot at
    `bundle.S[:, -1]` (the bundle's last column) -- both wrong when a bundle outlives the
    option. This forges a second bundle identical up to the option's own expiry index but
    with garbage afterward, and asserts the ledger is unchanged -- proving `cashflows()` never
    reads past its own expiry, rather than merely agreeing statistically on average."""
    engine = EngineConfig(scheme="qe", n_steps=20, n_paths=4, expiry=2.0, antithetic=False)
    bundle = simulate(_AAPL, engine, PseudoRandomSource(seed=1, antithetic=False))
    expiry_idx = 10  # t=1.0 on this 20-step, 2-year (dt=0.1) grid

    opt = BarrierOption(
        underlying="AAPL",
        option_type="call",
        strike=_AAPL.s0,
        expiry=1.0,
        barrier=1.0,  # unreachable: isolates the terminal-leg indexing, not survival
        direction="down",
        knock="out",
        monitoring=Monitoring.CONTINUOUS_BRIDGE,
        observations=None,
    )
    ledger = opt.cashflows(bundle)

    forged_s = bundle.S.copy()
    forged_s[:, expiry_idx + 1 :] = -999.0  # would corrupt bundle.S[:, -1] if read directly
    forged_bundle = replace(bundle, S=forged_s)
    forged_ledger = opt.cashflows(forged_bundle)

    np.testing.assert_array_equal(ledger.amounts, forged_ledger.amounts)
