"""Helpers shared by the Greek validation gates and the sweep that calibrates their docstrings:
the BS-degenerate fixture, the SAME-BUMP finite-difference references, and the independent-seed
(non-CRN) twins used as the G-CRN denominator."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from exo.greeks.bump import BumpConfig, Estimate, Greeks, compute_greeks
from exo.models.analytic import bs_call_price, bs_put_price
from exo.models.estimator import mc_estimate
from exo.models.heston import PathBundle, simulate
from exo.models.params import EngineConfig, HestonParams
from exo.models.rng import PseudoRandomSource, RandomSource
from exo.products.pricer import discount
from exo.products.warrant import Warrant

SIGMA = 0.2
DEGENERATE = HestonParams(
    s0=100.0, r=0.02, q=0.01, v0=SIGMA**2, kappa=1.5, theta=SIGMA**2, xi=1e-4, rho=0.0
)
STRIKE = 100.0
EXPIRY = 1.0
N_STEPS = 50
N_PATHS = 40_000
INDEP_SEED_OFFSET = 10_000
# Production bump sizes (exo.toml [greeks]).
BUMPS = BumpConfig(
    bump_spot_rel=0.01,
    bump_vol_abs=0.005,
    bump_rate_abs=0.001,
    bump_div_abs=0.001,
    theta_days_per_year=365,
    max_rel_std_error=0.02,
)
GREEK_NAMES = ("delta", "gamma", "vega", "rho", "dq", "theta")
CRN_GREEKS = ("delta", "vega", "rho", "theta")

Opt = Literal["call", "put"]


def warrant(option: Opt) -> Warrant:
    return Warrant(
        underlying="X", option_type=option, strike=STRIKE, expiry=EXPIRY, ratio=1.0,
        exercise="european",
    )  # fmt: skip


def run_greeks(option: Opt, seed: int) -> Greeks:
    return compute_greeks(
        warrant(option),
        DEGENERATE,
        scheme="qe",
        n_steps_per_year=N_STEPS,
        n_paths=N_PATHS,
        seed=seed,
        bumps=BUMPS,
    )


def references(option: Opt) -> dict[str, float]:
    """The same finite differences the estimator takes, applied to the Black-Scholes closed form."""
    price = bs_call_price if option == "call" else bs_put_price
    p = DEGENERATE

    def bs(
        s0: float = p.s0, r: float = p.r, q: float = p.q, sigma: float = SIGMA, t: float = EXPIRY
    ) -> float:
        return price(s0=s0, strike=STRIKE, r=r, q=q, sigma=sigma, expiry=t)

    h_s = BUMPS.bump_spot_rel * p.s0
    h_v, h_r, h_q = BUMPS.bump_vol_abs, BUMPS.bump_rate_abs, BUMPS.bump_div_abs
    dt = EXPIRY / N_STEPS
    return {
        "delta": (bs(s0=p.s0 + h_s) - bs(s0=p.s0 - h_s)) / (2 * h_s),
        "gamma": (bs(s0=p.s0 + h_s) - 2 * bs() + bs(s0=p.s0 - h_s)) / h_s**2,
        "vega": (bs(sigma=SIGMA + h_v) - bs(sigma=SIGMA - h_v)) / (2 * h_v),
        "rho": (bs(r=p.r + h_r) - bs(r=p.r - h_r)) / (2 * h_r),
        "dq": (bs(q=p.q + h_q) - bs(q=p.q - h_q)) / (2 * h_q),
        "theta": (bs(t=EXPIRY - dt) - bs()) / (dt * BUMPS.theta_days_per_year),
    }


@dataclass(frozen=True)
class Legs:
    """Per-path discounted vectors of the legs the G-CRN denominators need."""

    bundle: PathBundle
    base: NDArray[np.float64]


def _vec(
    option: Opt, params: HestonParams, rng: RandomSource, n_steps: int, expiry: float
) -> tuple[PathBundle, NDArray[np.float64]]:
    engine = EngineConfig(
        scheme="qe", n_steps=n_steps, n_paths=N_PATHS, expiry=expiry, antithetic=True
    )
    bundle = simulate(params, engine, rng)
    pay = Warrant(
        underlying="X", option_type=option, strike=STRIKE, expiry=expiry, ratio=1.0,
        exercise="european",
    )  # fmt: skip
    return bundle, discount(pay.cashflows(bundle, r=params.r), r=params.r)


def se_independent(option: Opt, seed: int) -> dict[str, float]:
    """SE of delta, vega, rho and theta when the two legs are drawn from INDEPENDENT seeds
    (`seed` for the minus/base leg, `seed + 10_000` for the plus/aged leg): what a caller would
    get without common random numbers. Same estimator, same path count per leg."""
    p = DEGENERATE
    a, b = (
        PseudoRandomSource(seed, antithetic=True),
        PseudoRandomSource(seed + INDEP_SEED_OFFSET, antithetic=True),
    )
    h_s = BUMPS.bump_spot_rel * p.s0
    h_v, h_r = BUMPS.bump_vol_abs, BUMPS.bump_rate_abs
    dt = EXPIRY / N_STEPS
    pairing, d0 = _vec(option, p, a, N_STEPS, EXPIRY)
    _, su = _vec(option, p.model_copy(update={"s0": p.s0 + h_s}), b, N_STEPS, EXPIRY)
    _, sd = _vec(option, p.model_copy(update={"s0": p.s0 - h_s}), a, N_STEPS, EXPIRY)
    up = {"v0": (SIGMA + h_v) ** 2, "theta": (SIGMA + h_v) ** 2}
    dn = {"v0": (SIGMA - h_v) ** 2, "theta": (SIGMA - h_v) ** 2}
    _, vu = _vec(option, p.model_copy(update=up), b, N_STEPS, EXPIRY)
    _, vd = _vec(option, p.model_copy(update=dn), a, N_STEPS, EXPIRY)
    _, ru = _vec(option, p.model_copy(update={"r": p.r + h_r}), b, N_STEPS, EXPIRY)
    _, rd = _vec(option, p.model_copy(update={"r": p.r - h_r}), a, N_STEPS, EXPIRY)
    _, da = _vec(option, p, b, N_STEPS - 1, EXPIRY - dt)

    def se(x: NDArray[np.float64]) -> float:
        return mc_estimate(pairing, x).std_err

    return {
        "delta": se((su - sd) / (2 * h_s)),
        "vega": se((vu - vd) / (2 * h_v)),
        "rho": se((ru - rd) / (2 * h_r)),
        "theta": se((da - d0) / (dt * BUMPS.theta_days_per_year)),
    }


def se_theta_undropped(option: Opt, seed: int) -> float:
    """Theta SE when the aged leg uses the SAME seed but does NOT drop the first step's draws:
    the aged run then consumes shifted increments, which is nearly uncoupled from the base."""
    p = DEGENERATE
    rng = PseudoRandomSource(seed, antithetic=True)
    dt = EXPIRY / N_STEPS
    pairing, d0 = _vec(option, p, rng, N_STEPS, EXPIRY)
    _, da = _vec(option, p, rng, N_STEPS - 1, EXPIRY - dt)
    return mc_estimate(pairing, (da - d0) / (dt * BUMPS.theta_days_per_year)).std_err


def zscores(option: Opt, greeks: Greeks) -> dict[str, float]:
    ref = references(option)
    out: dict[str, float] = {}
    for name in GREEK_NAMES:
        est: Estimate = getattr(greeks, name)
        if est.std_err == 0.0 or not math.isfinite(est.std_err):
            raise ValueError(f"{option} {name}: degenerate std_err={est.std_err}")
        out[name] = (est.value - ref[name]) / est.std_err
    return out
