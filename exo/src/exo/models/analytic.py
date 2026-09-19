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

import math
from typing import Literal

import numpy as np
from scipy.special import ndtr  # type: ignore[import-untyped]  # scipy ships no py.typed marker

_OPTION_TYPES = ("call", "put")
# Deliberately duplicated from products/base.py's own `_DIRECTIONS`, not imported from it:
# models/ must never import products/ (see exo/CLAUDE.md's M2 rule). Do not "fix" this into a
# shared import.
_DIRECTIONS = ("down", "up")
_KNOCKS = ("out", "in")


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

    This is a GATE REFERENCE, not a payoff (exo/CLAUDE.md engine design constraint 5):
    `products/barrier.py` is the payoff (an MC cashflow ledger); this is the reusable analytic
    cross-check, parallel to `bs_call_price`/`bs_put_price` above and to `heston_vanilla_price`
    in `heston_cf.py`.

    Formulas from Haug, "The Complete Guide to Option Pricing Formulas" (2nd ed.), the
    Reiner-Rubinstein single-barrier table: knock-IN prices are computed directly from the
    A/B/C/D terms below; knock-OUT is derived via in-out parity (`out = vanilla - in`), a
    model-free payoff identity, rather than a second independent derivation.

    RAISES `ValueError` if `barrier` is already breached at inception (`s0 <= barrier` for
    `direction="down"`, `s0 >= barrier` for `"up"`) -- the Reiner-Rubinstein table assumes spot
    starts on the live side of the barrier; evaluating it on the wrong side returns a negative
    option price. The MC side (`products/base.barrier_survival`) handles an inception breach
    correctly (survival is exactly 0 from `t=0`), so this closed form errs on the side of
    refusing to answer rather than diverging from its own gate reference.

    Evidence coverage across the eight (direction, option_type, strike-vs-barrier) cells is
    uneven: down-call `K>H` is checked against a real Heston MC engine degenerated to BS (G4);
    up-call `K>H` and down-put `K<H` are pinned exactly against `bs_call_price`/`bs_put_price`;
    the remaining four cells rest solely on this module's own degenerate limits and seam
    continuity, which are
    self-referential. UP-BARRIER PUTS ARE THE WEAKEST-EVIDENCED CELLS -- the first P2.M2 gate
    that prices an up-barrier put should suspect this reference before its own MC.
    """
    if option_type not in _OPTION_TYPES:
        raise ValueError(f"option_type must be one of {_OPTION_TYPES}, got {option_type!r}")
    if direction not in _DIRECTIONS:
        raise ValueError(f"direction must be one of {_DIRECTIONS}, got {direction!r}")
    if knock not in _KNOCKS:
        raise ValueError(f"knock must be one of {_KNOCKS}, got {knock!r}")

    # NaN/Inf bypass every `<=`/`>=` guard below under IEEE-754 -- checked explicitly alongside
    # the range checks, not left to fall out of them.
    if not all(math.isfinite(x) for x in (s0, strike, barrier, expiry, sigma, r, q)):
        raise ValueError(
            f"s0, strike, barrier, expiry, sigma, r and q must all be finite, got "
            f"s0={s0}, strike={strike}, barrier={barrier}, expiry={expiry}, sigma={sigma}, "
            f"r={r}, q={q}"
        )
    if s0 <= 0.0 or strike <= 0.0 or barrier <= 0.0 or expiry <= 0.0 or sigma <= 0.0:
        raise ValueError(
            f"s0, strike, barrier, expiry and sigma must all be positive, got "
            f"s0={s0}, strike={strike}, barrier={barrier}, expiry={expiry}, sigma={sigma}"
        )
    if (direction == "down" and s0 <= barrier) or (direction == "up" and s0 >= barrier):
        raise ValueError(
            f"barrier {barrier} is already breached at inception (s0={s0}, "
            f"direction={direction!r}) -- the Reiner-Rubinstein formulas assume s0 is on the "
            "live side of the barrier; see this function's own docstring."
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
