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
