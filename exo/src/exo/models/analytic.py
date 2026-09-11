"""Black-Scholes closed-form European option prices.

Used as the reference pricer for two things: this module's own unit tests, and the Heston
characteristic-function pricer's degenerate-limit cross-check (`exo/tests/test_heston_cf.py`) --
letting `xi -> 0` with `v0 = theta = sigma**2` collapses Heston to Black-Scholes, so any agreement
gap there is either genuine Heston-CF numerical residual or a formulation bug, not a missing
independent reference.

Normal CDF uses `scipy.special.ndtr` (vectorized C implementation) rather than `scipy.stats.norm`,
which carries a `rv_continuous` object-oriented dispatch layer this hot path does not need.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
from scipy.special import ndtr  # type: ignore[import-untyped]  # scipy ships no py.typed marker


def _norm_cdf(x: float) -> float:
    return float(ndtr(x))


def bs_call_price(
    *, s0: float, strike: float, r: float, q: float, sigma: float, expiry: float
) -> float:
    """European call price under Black-Scholes.

    Degenerate limits are handled explicitly rather than left to fall out of the general formula
    as NaN: `expiry == 0` is intrinsic value; `sigma == 0` is the discounted intrinsic value of the
    deterministic forward `F = s0 * exp((r - q) * expiry)`.
    """
    if expiry == 0.0:
        return max(s0 - strike, 0.0)
    if sigma == 0.0:
        forward = s0 * np.exp((r - q) * expiry)
        return float(np.exp(-r * expiry) * max(forward - strike, 0.0))
    d1, d2 = _d1_d2(s0, strike, r, q, sigma, expiry)
    return float(
        s0 * np.exp(-q * expiry) * _norm_cdf(d1) - strike * np.exp(-r * expiry) * _norm_cdf(d2)
    )


def bs_put_price(
    *, s0: float, strike: float, r: float, q: float, sigma: float, expiry: float
) -> float:
    """European put price under Black-Scholes. See `bs_call_price` for the degenerate limits."""
    if expiry == 0.0:
        return max(strike - s0, 0.0)
    if sigma == 0.0:
        forward = s0 * np.exp((r - q) * expiry)
        return float(np.exp(-r * expiry) * max(strike - forward, 0.0))
    d1, d2 = _d1_d2(s0, strike, r, q, sigma, expiry)
    return float(
        strike * np.exp(-r * expiry) * _norm_cdf(-d2) - s0 * np.exp(-q * expiry) * _norm_cdf(-d1)
    )


def _d1_d2(
    s0: float, strike: float, r: float, q: float, sigma: float, expiry: float
) -> tuple[float, float]:
    sqrt_t = np.sqrt(expiry)
    d1 = (np.log(s0 / strike) + (r - q + 0.5 * sigma**2) * expiry) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    return float(d1), float(d2)


def bs_barrier_price(
    *,
    s0: float,
    strike: float,
    r: float,
    q: float,
    sigma: float,
    expiry: float,
    barrier: float,
    direction: Literal["down", "up"],
    knock: Literal["out", "in"],
    option_type: Literal["call", "put"],
) -> float:
    """Reiner-Rubinstein (1991) CONTINUOUS-monitoring single barrier option, no rebate.

    THIS IS A GATE REFERENCE, NOT A PAYOFF (exo/CLAUDE.md engine design constraint 5, same
    reasoning that put `heston_vanilla_price` in `models/` rather than a test module): P2.M1's
    blocking barrier gate is "vs the BS closed form with the model degenerated to BS", and
    that comparison needs a real continuous-monitoring closed form, not copied magic numbers.
    A reviewer should not read this as an M2-abstraction-rule violation -- `products/barrier.py`
    is the PAYOFF (an MC cashflow ledger); this is the reusable ANALYTIC cross-check, exactly
    parallel to `bs_call_price`/`bs_put_price` above and to `heston_vanilla_price` in
    `heston_cf.py`.

    Formulas from Haug, "The Complete Guide to Option Pricing Formulas" (2nd ed.), the
    Reiner-Rubinstein single-barrier table: knock-IN prices are computed directly from the
    A/B/C/D terms below (split on `strike` vs `barrier`); knock-OUT is derived via in-out
    parity (`out = vanilla - in`), which is a MODEL-FREE payoff identity (every path either
    breaches, in which case the KI leg pays the vanilla payoff and the KO leg pays 0, or it
    doesn't, in which case the reverse holds -- so KO+KI==vanilla by construction regardless
    of which side is computed directly) rather than a second independent derivation.

    Validate this function by IDENTITY, never by copied magic numbers (see
    `exo/tests/test_analytic_barrier.py`): `DO+DI==vanilla` (and up-), `H->0` gives the
    vanilla, `H->S0` drives the knock-out to ~0. The real correctness check on the A/B/C/D
    algebra itself is `test_validation_gates_products.py`'s G4 gate (vs the Heston MC engine
    degenerated to BS) -- the in-out identity alone cannot catch a sign error in the knock-IN
    formula, since `out := vanilla - in` satisfies that identity trivially for ANY `in`.
    """
    if s0 <= 0.0 or strike <= 0.0 or barrier <= 0.0 or expiry <= 0.0 or sigma <= 0.0:
        raise ValueError(
            f"s0, strike, barrier, expiry and sigma must all be positive, got "
            f"s0={s0}, strike={strike}, barrier={barrier}, expiry={expiry}, sigma={sigma}"
        )

    b = r - q
    sqrt_t = np.sqrt(expiry)
    mu = (b - 0.5 * sigma**2) / sigma**2

    x1 = np.log(s0 / strike) / (sigma * sqrt_t) + (1.0 + mu) * sigma * sqrt_t
    x2 = np.log(s0 / barrier) / (sigma * sqrt_t) + (1.0 + mu) * sigma * sqrt_t
    y1 = np.log(barrier**2 / (s0 * strike)) / (sigma * sqrt_t) + (1.0 + mu) * sigma * sqrt_t
    y2 = np.log(barrier / s0) / (sigma * sqrt_t) + (1.0 + mu) * sigma * sqrt_t

    phi = 1.0 if option_type == "call" else -1.0
    eta = 1.0 if direction == "down" else -1.0

    def _term(x: float) -> float:
        return float(
            phi * s0 * np.exp((b - r) * expiry) * _norm_cdf(phi * x)
            - phi * strike * np.exp(-r * expiry) * _norm_cdf(phi * x - phi * sigma * sqrt_t)
        )

    def _term_hs(y: float) -> float:
        hs_price = (barrier / s0) ** (2.0 * (mu + 1.0))
        hs_bond = (barrier / s0) ** (2.0 * mu)
        return float(
            phi * s0 * np.exp((b - r) * expiry) * hs_price * _norm_cdf(eta * y)
            - phi
            * strike
            * np.exp(-r * expiry)
            * hs_bond
            * _norm_cdf(eta * y - eta * sigma * sqrt_t)
        )

    a_term = _term(x1)
    b_term = _term(x2)
    c_term = _term_hs(y1)
    d_term = _term_hs(y2)

    if option_type == "call":
        if direction == "down":
            in_price = c_term if strike > barrier else (a_term - b_term + d_term)
        else:
            in_price = a_term if strike > barrier else (b_term - c_term + d_term)
    else:
        if direction == "down":
            in_price = (b_term - c_term + d_term) if strike > barrier else a_term
        else:
            in_price = (a_term - b_term + d_term) if strike > barrier else c_term

    if knock == "in":
        return float(in_price)

    vanilla = (
        bs_call_price(s0=s0, strike=strike, r=r, q=q, sigma=sigma, expiry=expiry)
        if option_type == "call"
        else bs_put_price(s0=s0, strike=strike, r=r, q=q, sigma=sigma, expiry=expiry)
    )
    return float(vanilla - in_price)
