"""Tests for exo.models.heston_cf: the Heston vanilla pricer via characteristic function.

This is the slice's BLOCKING GATE (see the P2.M1 slice 1 spec): letting `xi -> 0` with
`v0 = theta = sigma**2` collapses the Heston dynamics to Black-Scholes, so agreement between
`heston_vanilla_price` and `exo.models.analytic.bs_call_price` in that limit is a cross-check on
the whole characteristic-function/quadrature pipeline, not just a unit test.
"""

import math
from dataclasses import dataclass

from hypothesis import given
from hypothesis import strategies as st

from exo.models.analytic import bs_call_price
from exo.models.heston_cf import (
    _EPSABS,
    heston_call_price_and_error,
    heston_vanilla_price,
)


@dataclass(frozen=True)
class _Params:
    """Minimal stand-in for `exo.models.params.HestonParams` (Track B).

    `heston_cf.py` accepts anything structurally matching `HestonParamsLike` -- it must not import
    Track B's pydantic model, since this pricer is a reusable component (slice 3's control
    variate, P2.M2's blocking gate too), not tied to one concrete parameter type.
    """

    s0: float
    r: float
    q: float
    v0: float
    kappa: float
    theta: float
    xi: float
    rho: float


# --- BLOCKING GATE: xi -> 0, v0 = theta = sigma**2 collapses Heston to Black-Scholes ------------


def test_cf_matches_bs_in_degenerate_bs_limit() -> None:
    sigma = 0.20
    params = _Params(
        s0=100.0, r=0.02, q=0.01, v0=sigma**2, kappa=1.5, theta=sigma**2, xi=1e-6, rho=-0.7
    )
    cf_price = heston_vanilla_price(params, strike=100.0, expiry=1.0, is_call=True)
    bs_price = bs_call_price(s0=100.0, strike=100.0, r=0.02, q=0.01, sigma=sigma, expiry=1.0)
    # Spec's planning-spike target: CF 8.34938303 vs BS 8.34940577 (agreement to 2.3e-5, residual
    # being xi=1e-6 not exactly 0). This implementation's rho/kappa choice differs from whatever
    # the spike used (unspecified by the spec), so the digits differ, but the residual must stay
    # at that same tiny (xi-driven-noise) scale -- a broken formulation (e.g. the branch-cut
    # crossing original Heston form) is wrong by orders of magnitude more, not by 1e-5.
    assert abs(cf_price - bs_price) < 5e-5


def test_cf_quadrature_error_estimate_is_within_requested_tolerance() -> None:
    """Amendment A5: a quadrature that silently failed to converge would still return a
    plausible-looking wrong price -- the same failure mode as the branch-cut bug. Assert
    `quad_vec`'s own `abserr` is bounded by the `epsabs` this module requests, so the fixed
    integration limits' ponytail marker is backed by a checked error estimate, not hope.

    Uses ordinary (non-degenerate) Heston parameters, not the xi=1e-6 BS-limit case above:
    measured directly, that degenerate case's `a/xi**2` prefactor (a = kappa*theta, with xi near
    machine-epsilon relative to it) amplifies floating-point rounding to an ~7e-6 noise floor that
    no amount of tightening `epsabs`/`epsrel` improves -- confirmed by sweeping epsabs from 1e-4 to
    1e-10 and observing abserr stay pinned in the same 1e-6-ish band throughout. That is a property
    of the xi->0 corner itself (this module's ponytail marker already scopes fixed tolerances to
    "the equity parameter ranges this slice"), not a quadrature that failed to converge on a
    realistic input -- which is what this assertion is meant to catch.
    """
    params = _Params(s0=100.0, r=0.02, q=0.01, v0=0.04, kappa=1.5, theta=0.04, xi=0.6, rho=-0.7)
    _, abserr = heston_call_price_and_error(params, strike=100.0, expiry=1.0)
    assert abserr < _EPSABS


# --- put-call parity on the CF price -------------------------------------------------------------


def test_cf_put_call_parity() -> None:
    params = _Params(s0=100.0, r=0.02, q=0.01, v0=0.04, kappa=1.5, theta=0.04, xi=0.6, rho=-0.7)
    call = heston_vanilla_price(params, strike=100.0, expiry=1.0, is_call=True)
    put = heston_vanilla_price(params, strike=100.0, expiry=1.0, is_call=False)
    forward_parity = params.s0 * math.exp(-params.q * 1.0) - 100.0 * math.exp(-params.r * 1.0)
    assert abs((call - put) - forward_parity) < 1e-6


# --- monotone decreasing in strike for a call ----------------------------------------------------


@given(
    strike_low=st.floats(min_value=50.0, max_value=95.0),
    strike_high=st.floats(min_value=105.0, max_value=150.0),
)
def test_cf_call_price_monotone_decreasing_in_strike(strike_low: float, strike_high: float) -> None:
    params = _Params(s0=100.0, r=0.02, q=0.01, v0=0.04, kappa=1.5, theta=0.04, xi=0.6, rho=-0.7)
    price_low = heston_vanilla_price(params, strike=strike_low, expiry=1.0, is_call=True)
    price_high = heston_vanilla_price(params, strike=strike_high, expiry=1.0, is_call=True)
    assert price_low >= price_high - 1e-8


# --- little-trap branch-cut guard: stays finite/no-arbitrage at a maturity where the original ---
# --- Heston formulation (g, not the little-trap c = 1/g) crosses the complex-log branch cut. ----


def test_little_trap_stays_within_no_arbitrage_bounds_at_long_maturity() -> None:
    """The original (non-trap) Heston CF crosses the complex-log branch cut and silently returns
    a wrong price at long maturities / high vol-of-vol -- in a scratch comparison for this slice,
    the naive form returned 0.31 instead of ~17.2 at T=5y and NaN at T=10y/20y for these
    parameters, while the little-trap form stayed finite throughout. This test does not re-derive
    that comparison (there is no reference price for a T=10y Heston vanilla without a second
    pricer); it pins the one property a broken formulation would violate here: the price must stay
    inside the model-free no-arbitrage band `[max(0, S0*e^-qT - K*e^-rT), S0*e^-qT]`.
    """
    params = _Params(s0=100.0, r=0.02, q=0.01, v0=0.04, kappa=1.5, theta=0.04, xi=0.6, rho=-0.7)
    strike, expiry = 100.0, 10.0
    price = heston_vanilla_price(params, strike=strike, expiry=expiry, is_call=True)
    discounted_spot = params.s0 * math.exp(-params.q * expiry)
    discounted_strike = strike * math.exp(-params.r * expiry)
    assert not math.isnan(price)
    assert max(0.0, discounted_spot - discounted_strike) - 1e-6 <= price <= discounted_spot + 1e-6
