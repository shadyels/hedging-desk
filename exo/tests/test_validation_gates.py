"""P2.M1 slice 1's BLOCKING validation gates (exo/CLAUDE.md: "MC vs closed-form agreement
within 3 standard errors is the acceptance test"; ADR-006 Amendment 4 s4).

- G1: MC (both schemes) vs `heston_vanilla_price` within 3 SE.
- G2: MC with the model degenerated to Black-Scholes (xi -> ~0, v0 = theta = sigma**2, rho = 0)
  vs the BS closed form within 3 SE.
- X : QE and Euler agree within combined SE on a fine step-count grid -- two schemes agreeing on
  one price catches a formulation bug a single-scheme suite cannot see.

EVERY 3-SE GATE HERE CARRIES A SECOND CONJUNCT BOUNDING THE SE ITSELF: `|mc - ref| < 3*se` is
vacuously true when `se` is large, so each gate also asserts `0 < se < tol_abs`. `tol_abs` is set
from the SE actually measured at this test's n_paths/n_steps (see the comment above each gate),
so the path count cannot later be quietly reduced until the gate stops meaning anything.

Payoffs are computed INLINE (slice 1 has NO payoff abstraction -- slice 2 designs `Payoff`
against two REAL products, per ADR-006 Amd 4 s3, which is the pressure that exposes a bad
abstraction). Do not DRY this three-line expression into `models/`.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from exo.models.analytic import bs_call_price
from exo.models.estimator import mc_estimate
from exo.models.heston import simulate
from exo.models.heston_cf import heston_vanilla_price
from exo.models.params import EngineConfig, HestonParams, Scheme
from exo.models.rng import PseudoRandomSource

# Feller-VIOLATING (2*1.5*0.04/0.36 = 0.333 < 1), shared with test_heston_paths.py's constant of
# the same values: this is the regime the two schemes actually differ in.
_FELLER_VIOLATING = HestonParams(
    s0=100.0, r=0.02, q=0.01, v0=0.04, kappa=1.5, theta=0.04, xi=0.6, rho=-0.7
)
_STRIKE = 100.0
_EXPIRY = 1.0


def _discounted_call_payoff(r: float, expiry: float, strike: float, s_t: np.ndarray) -> np.ndarray:
    # INLINE per slice 1's rule: no Payoff abstraction exists yet (see module docstring).
    return np.exp(-r * expiry) * np.maximum(s_t - strike, 0.0)


@pytest.mark.parametrize("scheme", ["qe", "euler-ft"])
def test_g1_mc_matches_heston_cf_within_3se(scheme: Scheme) -> None:
    """G1. Measured at n_steps=50, n_paths=20_000, seed=42: SE ~= 0.047 (qe) / 0.049 (euler-ft),
    |mc - ref|/se ~= 0.43 (qe) / 0.42 (euler-ft). tol_abs=0.06 sits just above both measured SEs."""
    engine = EngineConfig(
        scheme=scheme, n_steps=50, n_paths=20_000, expiry=_EXPIRY, antithetic=True
    )
    bundle = simulate(_FELLER_VIOLATING, engine, PseudoRandomSource(seed=42))
    payoff = _discounted_call_payoff(_FELLER_VIOLATING.r, _EXPIRY, _STRIKE, bundle.S[:, -1])
    result = mc_estimate(bundle, payoff)

    reference = heston_vanilla_price(_FELLER_VIOLATING, _STRIKE, _EXPIRY, is_call=True)

    tol_abs = 0.06
    assert 0.0 < result.std_err < tol_abs, (
        f"{scheme}: se={result.std_err} is not tight enough to be a meaningful gate "
        f"(tol_abs={tol_abs})"
    )
    assert abs(result.pv - reference) < 3.0 * result.std_err, (
        f"{scheme}: pv={result.pv}, ref={reference}, "
        f"off by {(result.pv - reference) / result.std_err:.2f} SE"
    )


@pytest.mark.parametrize("scheme", ["qe", "euler-ft"])
def test_g2_mc_matches_black_scholes_in_degenerate_limit_within_3se(scheme: Scheme) -> None:
    """G2. xi -> ~0, v0 = theta = sigma**2, rho = 0 collapses Heston to Black-Scholes. Measured
    at n_steps=50, n_paths=20_000, seed=123: SE ~= 0.0732 for both schemes, |mc - ref|/se ~= 0.47.
    tol_abs=0.09 sits just above the measured SE."""
    sigma = 0.2
    degenerate = HestonParams(
        s0=100.0, r=0.02, q=0.01, v0=sigma**2, kappa=1.5, theta=sigma**2, xi=1e-4, rho=0.0
    )
    engine = EngineConfig(
        scheme=scheme, n_steps=50, n_paths=20_000, expiry=_EXPIRY, antithetic=True
    )
    bundle = simulate(degenerate, engine, PseudoRandomSource(seed=123))
    payoff = _discounted_call_payoff(degenerate.r, _EXPIRY, _STRIKE, bundle.S[:, -1])
    result = mc_estimate(bundle, payoff)

    reference = bs_call_price(
        s0=100.0, strike=_STRIKE, r=degenerate.r, q=degenerate.q, sigma=sigma, expiry=_EXPIRY
    )

    tol_abs = 0.09
    assert 0.0 < result.std_err < tol_abs, (
        f"{scheme}: se={result.std_err} is not tight enough to be a meaningful gate "
        f"(tol_abs={tol_abs})"
    )
    assert abs(result.pv - reference) < 3.0 * result.std_err, (
        f"{scheme}: pv={result.pv}, bs_ref={reference}, "
        f"off by {(result.pv - reference) / result.std_err:.2f} SE"
    )


def test_x_qe_and_euler_agree_within_combined_se_on_fine_grid() -> None:
    """X. Measured at n_steps=500, n_paths=20_000, shared seed=77: combined SE ~= 0.0689,
    diff/combined_se ~= -1.13. tol_abs=0.09 sits just above the measured combined SE. Two
    independently-derived schemes agreeing on one price catches a formulation bug (e.g. a wrong
    martingale correction, a sign error in a drift term) that a single-scheme suite, cross-checked
    only against its own reference derivation, cannot see."""
    results = {}
    for scheme in ("qe", "euler-ft"):
        engine = EngineConfig(
            scheme=scheme, n_steps=500, n_paths=20_000, expiry=_EXPIRY, antithetic=True
        )
        bundle = simulate(_FELLER_VIOLATING, engine, PseudoRandomSource(seed=77))
        payoff = _discounted_call_payoff(_FELLER_VIOLATING.r, _EXPIRY, _STRIKE, bundle.S[:, -1])
        results[scheme] = mc_estimate(bundle, payoff)

    qe, euler = results["qe"], results["euler-ft"]
    combined_se = math.sqrt(qe.std_err**2 + euler.std_err**2)

    tol_abs = 0.09
    assert 0.0 < combined_se < tol_abs, (
        f"combined_se={combined_se} is not tight enough to be a meaningful gate (tol_abs={tol_abs})"
    )
    assert abs(qe.pv - euler.pv) < 3.0 * combined_se, (
        f"qe.pv={qe.pv}, euler.pv={euler.pv}, "
        f"off by {(qe.pv - euler.pv) / combined_se:.2f} combined SE"
    )
