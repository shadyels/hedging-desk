"""Tests for exo.models.control_variate (P2.M1 slice 3, T5).

The crux under test is the beta decision documented in `estimate_beta`'s docstring: beta must
never be fitted on the sample being priced, so `price_from_bundle(control=...)` without `beta`
raises rather than silently defaulting to an unsafe same-sample OLS estimate.
"""

from __future__ import annotations

import numpy as np
import pytest

from exo.models.control_variate import (
    ControlVariate,
    apply_control,
    estimate_beta,
    vanilla_control,
)
from exo.models.estimator import mc_estimate
from exo.models.heston import PathBundle, simulate
from exo.models.heston_cf import heston_vanilla_price
from exo.models.params import EngineConfig, HestonParams
from exo.models.rng import PseudoRandomSource
from exo.products.barrier import BarrierOption
from exo.products.base import Monitoring
from exo.products.pricer import price_from_bundle

_AAPL = HestonParams(
    s0=187.50, r=0.0425, q=0.0050, v0=0.0400, kappa=1.50, theta=0.0400, xi=0.60, rho=-0.70
)


def _bundle(seed: int, n_paths: int = 2000, n_steps: int = 20, expiry: float = 1.0) -> PathBundle:
    engine = EngineConfig(
        scheme="qe", n_steps=n_steps, n_paths=n_paths, expiry=expiry, antithetic=True
    )
    return simulate(_AAPL, engine, PseudoRandomSource(seed=seed))


def test_exact_identity_zero_variance_when_control_equals_payoff() -> None:
    """A ControlVariate whose payoff IS the discounted payoff being priced, and whose mean is
    that payoff's true mean: with beta=1.0, apply_control returns the constant vector `mean`
    EXACTLY (asserted below via bare `==`, safe here because it is a constant-vector identity,
    not an MC comparison), so pv == mean exactly and std_err is at machine-precision noise
    (numpy's mean/variance reduction over a constant array is not bit-exact zero) -- a sign or
    centring error would fail loudly here, not statistically."""
    bundle = _bundle(seed=1)
    strike, expiry, r = 190.0, 1.0, _AAPL.r
    payoff = np.exp(-r * expiry) * np.maximum(bundle.S[:, -1] - strike, 0.0)
    true_mean = heston_vanilla_price(_AAPL, strike, expiry, is_call=True)
    cv = ControlVariate(payoff=payoff, mean=true_mean)

    controlled = apply_control(payoff, cv, beta=1.0)
    assert np.all(controlled == true_mean)

    result = mc_estimate(bundle, controlled)
    assert result.pv == pytest.approx(true_mean)
    assert result.std_err < 1e-10


def test_beta_zero_reproduces_uncontrolled_result_exactly() -> None:
    bundle = _bundle(seed=2)
    strike, expiry, r = 190.0, 1.0, _AAPL.r
    payoff = np.exp(-r * expiry) * np.maximum(bundle.S[:, -1] - strike, 0.0)
    cv = ControlVariate(payoff=payoff * 2.0 + 1.0, mean=123.456)

    uncontrolled = mc_estimate(bundle, payoff)
    controlled = mc_estimate(bundle, apply_control(payoff, cv, beta=0.0))

    assert controlled.pv == uncontrolled.pv
    assert controlled.std_err == uncontrolled.std_err


def test_estimate_beta_recovers_exact_slope_on_noise_free_data() -> None:
    rng = np.random.default_rng(0)
    x = rng.normal(size=10_000)
    c = 3.7
    payoff = 2.0 * x + c
    assert estimate_beta(payoff, x) == pytest.approx(2.0, abs=1e-12)


def test_estimate_beta_raises_on_zero_variance_control() -> None:
    payoff = np.array([1.0, 2.0, 3.0])
    control = np.array([5.0, 5.0, 5.0])
    with pytest.raises(ValueError):
        estimate_beta(payoff, control)


def test_price_from_bundle_requires_beta_when_control_supplied() -> None:
    bundle = _bundle(seed=3)
    option = BarrierOption(
        underlying="AAPL",
        option_type="call",
        strike=190.0,
        expiry=1.0,
        barrier=150.0,
        direction="down",
        knock="out",
        monitoring=Monitoring.CONTINUOUS_BRIDGE,
    )
    cv = vanilla_control(bundle, _AAPL, strike=190.0, expiry=1.0, r=_AAPL.r)
    with pytest.raises(ValueError):
        price_from_bundle(option, bundle, r=_AAPL.r, control=cv)


def test_apply_control_raises_on_length_mismatch() -> None:
    payoff = np.array([1.0, 2.0, 3.0])
    cv = ControlVariate(payoff=np.array([1.0, 2.0]), mean=0.0)
    with pytest.raises(ValueError):
        apply_control(payoff, cv, beta=1.0)


def test_vanilla_control_shape_and_mean_match_heston_cf() -> None:
    bundle = _bundle(seed=4)
    strike, expiry, r = 190.0, 1.0, _AAPL.r
    cv = vanilla_control(bundle, _AAPL, strike=strike, expiry=expiry, r=r)
    assert cv.payoff.shape == (bundle.S.shape[0],)
    assert cv.mean == heston_vanilla_price(_AAPL, strike, expiry, is_call=True)


def test_control_variate_reduces_variance_on_barrier_gate_config() -> None:
    """G4-style down-and-out call, CONTINUOUS_BRIDGE, under real (non-degenerate) AAPL params.
    Beta is estimated on a SEPARATELY SEEDED bundle (respecting the disjoint-sample rule) and
    applied to the bundle actually being priced. Fixed seeds throughout."""
    strike, barrier, expiry = 190.0, 150.0, 1.0
    n_steps, n_paths = 50, 20_000
    r = _AAPL.r

    option = BarrierOption(
        underlying="AAPL",
        option_type="call",
        strike=strike,
        expiry=expiry,
        barrier=barrier,
        direction="down",
        knock="out",
        monitoring=Monitoring.CONTINUOUS_BRIDGE,
    )

    pilot_bundle = _bundle(seed=999, n_paths=n_paths, n_steps=n_steps, expiry=expiry)
    pilot_ledger = option.cashflows(pilot_bundle)
    from exo.products.pricer import discount

    pilot_payoff = discount(pilot_ledger, r=r)
    pilot_control = vanilla_control(pilot_bundle, _AAPL, strike=strike, expiry=expiry, r=r)
    beta = estimate_beta(pilot_payoff, pilot_control.payoff)

    priced_bundle = _bundle(seed=1, n_paths=n_paths, n_steps=n_steps, expiry=expiry)
    uncontrolled = price_from_bundle(option, priced_bundle, r=r)

    priced_control = vanilla_control(priced_bundle, _AAPL, strike=strike, expiry=expiry, r=r)
    controlled = price_from_bundle(option, priced_bundle, r=r, control=priced_control, beta=beta)

    ratio = uncontrolled.std_err / controlled.std_err
    assert controlled.std_err < uncontrolled.std_err, (
        f"control variate did not reduce variance: se_uncontrolled={uncontrolled.std_err}, "
        f"se_controlled={controlled.std_err}, ratio={ratio}"
    )
