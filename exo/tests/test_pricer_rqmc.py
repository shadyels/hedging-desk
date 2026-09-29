"""Tests for exo.products.pricer.price_rqmc — the RQMC pricing entry point (Slice 3 T4).

Uses a knock-out call with the barrier set far above spot as a vanilla call stand-in
(never crossed, so `weight == 1` and the payoff is exactly `max(S_T - K, 0)`) --
avoids writing a second payoff type just for these tests.
"""

from __future__ import annotations

import numpy as np
import pytest

from exo.models.heston_cf import heston_vanilla_price
from exo.models.params import EngineConfig, HestonParams
from exo.models.rng import SobolRandomSource
from exo.products.barrier import BarrierOption
from exo.products.base import Monitoring
from exo.products.pricer import price_rqmc

_PARAMS = HestonParams(s0=100.0, r=0.02, q=0.01, v0=0.04, kappa=1.5, theta=0.04, xi=0.6, rho=-0.7)
_STRIKE = 100.0
_EXPIRY = 1.0


def _vanilla_call(expiry: float = _EXPIRY, strike: float = _STRIKE) -> BarrierOption:
    return BarrierOption(
        underlying="TEST",
        option_type="call",
        strike=strike,
        expiry=expiry,
        barrier=1.0e6,  # never crossed -> survival == 1 always -> plain vanilla payoff
        direction="up",
        knock="out",
        monitoring=Monitoring.CONTINUOUS_BRIDGE,
    )


def _up_and_out_call(expiry: float = _EXPIRY, strike: float = _STRIKE) -> BarrierOption:
    """A REAL barrier (reachable at these params/expiry): correlated with, but not
    identical to, the vanilla-call control -- unlike `_vanilla_call`, whose payoff
    degenerately equals the control's payoff exactly and collapses `apply_control`'s
    output to a zero-variance constant."""
    return BarrierOption(
        underlying="TEST",
        option_type="call",
        strike=strike,
        expiry=expiry,
        barrier=130.0,
        direction="up",
        knock="out",
        monitoring=Monitoring.CONTINUOUS_BRIDGE,
    )


def _engine(n_paths: int = 1024, n_steps: int = 16, antithetic: bool = False) -> EngineConfig:
    return EngineConfig(
        scheme="qe", n_steps=n_steps, n_paths=n_paths, expiry=_EXPIRY, antithetic=antithetic
    )


def test_price_rqmc_raises_on_antithetic_engine() -> None:
    payoff = _vanilla_call()
    engine = _engine(antithetic=True)
    with pytest.raises(ValueError, match="Amendment 6"):
        price_rqmc(payoff, _PARAMS, engine, seed=1, n_replicates=8)


def test_price_rqmc_raises_on_non_power_of_two_n_paths() -> None:
    payoff = _vanilla_call()
    engine = _engine(n_paths=1000)
    with pytest.raises(ValueError, match="power of two"):
        price_rqmc(payoff, _PARAMS, engine, seed=1, n_replicates=8)


def test_price_rqmc_deterministic() -> None:
    payoff = _vanilla_call()
    engine = _engine(n_paths=256, n_steps=8)
    result_a = price_rqmc(payoff, _PARAMS, engine, seed=42, n_replicates=8)
    result_b = price_rqmc(payoff, _PARAMS, engine, seed=42, n_replicates=8)
    assert result_a.pv == result_b.pv
    assert result_a.std_err == result_b.std_err


def test_crn_bitwise_identical_across_bump_pair() -> None:
    """The P2.M4 property, asserted directly on the Sobol source (as the brief allows):
    a base params set and a bumped one, priced with the same (seed, n_paths, dims,
    replicate), draw bitwise-identical Sobol point sets -- the bump only changes what
    the SIMULATION does with those numbers, never the numbers themselves."""
    dims = (("variance", 8), ("spot", 8))
    base = SobolRandomSource(seed=42, n_paths=256, dims=dims, replicate=0)
    bumped = SobolRandomSource(seed=42, n_paths=256, dims=dims, replicate=0)
    assert np.array_equal(
        base.normals((256, 8), stream="spot"), bumped.normals((256, 8), stream="spot")
    )
    assert np.array_equal(
        base.uniforms((256, 8), stream="variance"), bumped.uniforms((256, 8), stream="variance")
    )


def test_price_rqmc_control_variate_reduces_std_err() -> None:
    payoff = _up_and_out_call()
    engine = _engine(n_paths=512, n_steps=8)
    plain = price_rqmc(payoff, _PARAMS, engine, seed=7, n_replicates=16)
    controlled = price_rqmc(
        payoff, _PARAMS, engine, seed=7, n_replicates=16, control_strike=_STRIKE
    )
    assert np.isfinite(controlled.pv)
    assert np.isfinite(controlled.std_err)
    assert controlled.std_err < plain.std_err


def test_price_rqmc_agrees_with_closed_form_within_3se() -> None:
    """No control here: the payoff itself is a plain vanilla call, so it is compared
    directly against the closed-form Heston vanilla price -- adding this payoff's own
    control would make it identical to the control (see `_up_and_out_call`'s docstring
    for why that degenerates `apply_control` to a zero-variance constant)."""
    payoff = _vanilla_call()
    engine = _engine(n_paths=1024, n_steps=16)
    result = price_rqmc(payoff, _PARAMS, engine, seed=99, n_replicates=64)
    reference = heston_vanilla_price(_PARAMS, _STRIKE, _EXPIRY, is_call=True)
    assert result.std_err > 0.0
    assert abs(result.pv - reference) < 3 * result.std_err
