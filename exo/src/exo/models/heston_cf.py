"""Heston vanilla-option pricer via the characteristic function (Gatheral "little trap").

Slice 1's BLOCKING GATE: with `xi -> 0` and `v0 = theta = sigma**2`, the Heston dynamics collapse
to Black-Scholes, so `heston_vanilla_price` agreeing with `exo.models.analytic.bs_call_price` in
that limit cross-checks the whole characteristic-function/quadrature pipeline (exo/CLAUDE.md:
"Every priced product must have an analytic or semi-analytic cross-check test").

This ships as a PUBLIC PRICER, not a test helper (exo/CLAUDE.md engine design constraint 5): there
is no closed form for a barrier under Heston, so this same characteristic function is the natural
control variate for slice 3's variance reduction and P2.M2's warrant blocking gate. Burying it in
a test module would mean writing it twice.

THE "LITTLE TRAP" FORMULATION IS NOT STYLISTIC. Heston's original (1993) characteristic function
requires evaluating a principal complex logarithm whose argument winds around the branch cut once
`u * T` (integration variable times time-to-expiry) grows large enough -- longer maturities, higher
vol-of-vol, or stronger correlation all push it there. Each winding is a silent discontinuous jump:
scipy's adaptive quadrature integrates straight through it and returns a plausible-looking WRONG
price, no exception, no warning. Albrecher, Mayer, Schoutens & Tistaert (2007), "The Little Heston
Trap", show the fix is an algebraic change of variable -- replace the term `g` that carries the
branch-prone factor with its reciprocal `g2 = 1/g1` (called `c` below) -- which is the exact same
characteristic function, but whose complex log now stays inside the unit disk and never crosses
the cut. Measured for this slice (S0=K=100, r=2%, q=1%, v0=theta=4%, kappa=1.5, rho=-0.7): the
original formulation already disagrees with the little-trap one at T=5y (0.31 vs ~17.16) and
returns NaN outright at T=10y and T=20y, while the little-trap form stays finite and stable
throughout. `exo/tests/test_heston_cf.py`'s
`test_little_trap_stays_within_no_arbitrage_bounds_at_long_maturity` pins the corresponding
no-arbitrage invariant.

Reference for the algebra: Gatheral, "The Volatility Surface" (2006) s2.3, and Rouah, "The Heston
Model and Its Extensions in Matlab and C#" (2013), the "Little Trap" HestonProb formulation.
"""

from __future__ import annotations

from typing import Protocol

import numpy as np
from numpy.typing import NDArray
from scipy.integrate import quad_vec  # type: ignore[import-untyped]  # scipy has no py.typed marker


class HestonParamsLike(Protocol):
    """Structural protocol for the eight Heston parameters this pricer needs.

    Deliberately duck-typed rather than importing `exo.models.params.HestonParams`: this module is
    a reusable pricer (control variate for slice 3, P2.M2's blocking gate too), so it must not
    depend on Track B's concrete parameter type. Any object with these eight attributes --
    including the real `HestonParams` -- satisfies this Protocol structurally.
    """

    s0: float
    r: float
    q: float
    v0: float
    kappa: float
    theta: float
    xi: float
    rho: float


# ponytail: fixed integration limits (_U_MAX=1000.0, _EPSABS=_EPSREL=1e-10), no adaptive fallback.
# quad_vec's abserr covers [U_MIN, U_MAX] only and is BLIND to tail truncation. Ceiling: tail ~
# exp(-(v0+kappa*theta*T)*sqrt(1-rho^2)*U/xi)/U; at 1000 the tail truncation error is below
# 4e-10 for every configured set at T=1/12 against a U=20000 reference (worst 3.84e-10, SPX at
# 0.8*S0; ADR-006 Amendment 8 s5). Trigger: a parameter
# set with v0*sqrt(1-rho^2)/xi < 0.03 (e.g. P4.M4 calibration output), or the first gate failure
# traced to truncation. Second ceiling: xi so small that the a/xi**2 prefactor amplifies
# floating-point noise (abserr floor about 1e-6 at xi=1e-6).
_U_MIN = 1e-10
_U_MAX = 1000.0
_EPSABS = 1e-10
_EPSREL = 1e-10


def _little_trap_integrand(
    phi: complex,
    lnk: float,
    x0: float,
    r: float,
    q: float,
    v0: float,
    kappa: float,
    theta: float,
    xi: float,
    rho: float,
    expiry: float,
) -> NDArray[np.float64]:
    """Re[exp(-i*phi*ln K) * f_j(phi) / (i*phi)] for j=1,2, computed together (one vectorized
    2-element output per scalar `phi`, per the spec: "computing BOTH P1 and P2 in one vectorized
    integrand")."""
    a = kappa * theta
    u = np.array([0.5, -0.5], dtype=np.complex128)
    b = np.array([kappa - rho * xi, kappa], dtype=np.complex128)

    disc = np.sqrt((rho * xi * 1j * phi - b) ** 2 - xi**2 * (2 * u * 1j * phi - phi**2))
    g_ratio = (b - rho * xi * 1j * phi + disc) / (b - rho * xi * 1j * phi - disc)
    c_trap = 1.0 / g_ratio  # the "little trap": g2 = 1/g1
    exp_dt = np.exp(-disc * expiry)

    c_coef = (r - q) * 1j * phi * expiry + (a / xi**2) * (
        (b - rho * xi * 1j * phi - disc) * expiry
        - 2.0 * np.log((1 - c_trap * exp_dt) / (1 - c_trap))
    )
    d_coef = (b - rho * xi * 1j * phi - disc) / xi**2 * ((1 - exp_dt) / (1 - c_trap * exp_dt))
    f = np.exp(c_coef + d_coef * v0 + 1j * phi * x0)

    result: NDArray[np.float64] = np.real(np.exp(-1j * phi * lnk) * f / (1j * phi))
    return result


def heston_call_price_and_error(
    params: HestonParamsLike, strike: float, expiry: float
) -> tuple[float, float]:
    """The Heston call price and `scipy.integrate.quad_vec`'s own absolute-error estimate.

    Exposed separately from `heston_vanilla_price` (which returns just the price, per the spec's
    public signature) so callers -- and this module's own gate test -- can check
    quadrature convergence on [U_MIN, U_MAX]: amendment A5 requires the gate test to assert
    `abserr < epsabs`, since a quadrature that silently failed to converge would otherwise return a
    plausible-looking WRONG price. `abserr` does NOT cover truncation of the infinite range at
    `_U_MAX` (see the ponytail marker above; tail truncation is pinned by its own regression test).
    """
    if strike <= 0.0:
        raise ValueError(f"strike must be positive, got {strike}")
    if expiry <= 0.0:
        raise ValueError(f"expiry must be positive, got {expiry}")

    x0 = np.log(params.s0)
    lnk = np.log(strike)

    def integrand(phi: complex) -> NDArray[np.float64]:
        return _little_trap_integrand(
            phi,
            lnk,
            x0,
            params.r,
            params.q,
            params.v0,
            params.kappa,
            params.theta,
            params.xi,
            params.rho,
            expiry,
        )

    value, abserr = quad_vec(integrand, _U_MIN, _U_MAX, epsabs=_EPSABS, epsrel=_EPSREL)
    p1, p2 = 0.5 + value[0] / np.pi, 0.5 + value[1] / np.pi
    call = params.s0 * np.exp(-params.q * expiry) * p1 - strike * np.exp(-params.r * expiry) * p2
    return float(call), float(abserr)


def heston_vanilla_price(
    params: HestonParamsLike, strike: float, expiry: float, is_call: bool
) -> float:
    """European vanilla price under Heston, via the little-trap characteristic function.

    The put price is obtained from the call via put-call parity rather than a second quadrature
    (`P1`/`P2` are call-side probabilities; deriving the put from them directly would need its own
    pair of integrals).
    """
    call, _ = heston_call_price_and_error(params, strike, expiry)
    if is_call:
        return call
    return float(
        call - params.s0 * np.exp(-params.q * expiry) + strike * np.exp(-params.r * expiry)
    )
