"""CRN bump-and-revalue Greeks (ADR-006 Amendment 9 s2; exo/CLAUDE.md rule 6).

One product, one `(seed, n_paths)`: every leg is simulated from the SAME stateless
`PseudoRandomSource(seed, antithetic=True)` (named streams, `models/rng.py`), so a bumped leg
shares every draw with the base leg and only the perturbed input differs. Each leg is reduced at
once to its `(n_paths,)` discounted vector; each Greek is `mc_estimate(base_pairing, per-path
combination)`, so its standard error is the antithetic pair-mean SE of the per-path DIFFERENCE --
which is what makes it valid for a difference of correlated legs (it is far tighter than the SE of
two independent legs; `tests/test_validation_gates_greeks.py::test_g_crn_*` measures the ratio).

`Greeks` holds RAW per-unit derivatives of the product's own PV (not desk-signed, not wire units;
`portfolio/aggregate.py` scales and labels): delta = dPV/dS, gamma = d2PV/dS2, vega = dPV/d(sigma)
with sqrt(v0) and sqrt(theta) shifted together, rho = dPV/dr (moves drift AND discount), dq =
dPV/dq (labelled EQUITY dividend sensitivity or FX foreign rho by the aggregator, Amendment 9 s4).
The one exception is `theta`: it is per CALENDAR DAY already, because `theta_days_per_year` is a
`[greeks]` knob that belongs with the other bump settings (EXO configuration, not a market
convention -- Amendment 9 s2).

Theta is a coupled one-step "time passes, nothing else moves" estimator: the aged run has one step
fewer, every term-sheet time shifted by `dt` (`age_payoff`), and consumes the base run's draws from
step 2 onward (`DropLeadingStepsSource`). That is only possible with a column-sliceable stream, so
Sobol is refused.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from typing import Any, TypeVar, cast

import numpy as np
from numpy.typing import NDArray
from pydantic import ConfigDict, Field

from exo.models.estimator import mc_estimate
from exo.models.heston import PathBundle, simulate
from exo.models.params import EngineConfig, HestonParams, Scheme, StrictNumbersModel
from exo.models.rng import PseudoRandomSource, RandomSource
from exo.products.autocallable import Autocallable
from exo.products.barrier import BarrierOption
from exo.products.base import Payoff
from exo.products.bonus import BonusCertificate
from exo.products.pricer import discount
from exo.products.reverse_convertible import ReverseConvertible
from exo.products.tarf import Ptarf, Tarf
from exo.products.warrant import Warrant


class BumpConfig(StrictNumbersModel):
    """`exo.toml [greeks]`: bump sizes and the flagging threshold. Never hardcoded in code."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    bump_spot_rel: float = Field(gt=0, lt=1)  # relative: h_S * s0
    bump_vol_abs: float = Field(gt=0)  # absolute, on sqrt(v0) and sqrt(theta)
    bump_rate_abs: float = Field(gt=0)  # absolute, on r
    bump_div_abs: float = Field(gt=0)  # absolute, on q
    theta_days_per_year: float = Field(gt=0)
    max_rel_std_error: float = Field(gt=0)  # flagging only; not a bump size


@dataclass(frozen=True)
class Estimate:
    """A Monte Carlo estimate and its standard error: one return value (exo/CLAUDE.md rule 3)."""

    value: float
    std_err: float


@dataclass(frozen=True)
class Greeks:
    """Raw per-unit derivatives of one product's PV. See the module docstring for units."""

    pv: Estimate
    delta: Estimate
    gamma: Estimate
    vega: Estimate
    rho: Estimate
    dq: Estimate
    theta: Estimate  # per calendar day (theta_days_per_year)


@dataclass(frozen=True)
class DropLeadingStepsSource:
    """A `RandomSource` whose `(n, m)` draw is the base source's `(n, m + k)` draw minus its first
    `k` columns. The aged theta run therefore reuses the base run's increments from step `k + 1`
    onward (ADR-006 Amendment 9 s2). Named pseudo-random streams are column-sliceable; a Sobol net
    is not (its dimension would change), so a low-discrepancy base raises.
    """

    base: RandomSource
    k: int = 1

    def __post_init__(self) -> None:
        if self.base.low_discrepancy:
            raise ValueError(
                "DropLeadingStepsSource needs a column-sliceable pseudo-random base: a Sobol "
                "net's dimension changes with the step count, so the aged run would get a "
                "different net and no common random numbers (ADR-006 Amendment 9 s1)"
            )
        if self.k < 1:
            raise ValueError(f"k must be >= 1, got {self.k}")

    @property
    def seed(self) -> int:
        return self.base.seed

    @property
    def antithetic(self) -> bool:
        return self.base.antithetic

    @property
    def low_discrepancy(self) -> bool:
        return False

    def normals(self, shape: tuple[int, ...], *, stream: str) -> NDArray[np.float64]:
        return self.base.normals((shape[0], shape[1] + self.k), stream=stream)[:, self.k :]

    def uniforms(self, shape: tuple[int, ...], *, stream: str) -> NDArray[np.float64]:
        return self.base.uniforms((shape[0], shape[1] + self.k), stream=stream)[:, self.k :]


# Ageing table: for each loadable product class, the fields that are TIMES (shifted by -dt when
# the product ages one model step) and the fields that are not. A test asserts that together they
# cover every dataclass field, so a new time field cannot be silently left unaged.
_TIME_FIELDS: dict[type, tuple[str, ...]] = {
    Autocallable: ("observations", "expiry"),
    BarrierOption: ("expiry", "observations"),
    ReverseConvertible: ("coupon_times", "expiry", "observations"),
    BonusCertificate: ("expiry", "observations"),
    Warrant: ("expiry",),
    Tarf: ("fixings", "expiry"),
    Ptarf: ("fixings", "expiry"),
}
_NON_TIME_FIELDS: dict[type, tuple[str, ...]] = {
    Autocallable: (
        "underlying",
        "notional",
        "autocall_trigger",
        "coupon_barrier",
        "coupon_rate",
        "memory",
        "protection_barrier",
        "initial_level",
    ),
    BarrierOption: (
        "underlying",
        "option_type",
        "strike",
        "barrier",
        "direction",
        "knock",
        "monitoring",
    ),
    ReverseConvertible: (
        "underlying",
        "notional",
        "strike",
        "coupon_rate",
        "barrier",
        "monitoring",
    ),
    BonusCertificate: ("underlying", "ratio", "bonus_level", "barrier", "monitoring"),
    Warrant: ("underlying", "option_type", "strike", "ratio", "exercise"),
    Tarf: ("underlying", "notional", "strike", "leverage", "target"),
    Ptarf: (
        "underlying",
        "notional",
        "strike_low",
        "pivot",
        "strike_high",
        "leverage",
        "target",
    ),
}

AGEING_TIME_FIELDS = _TIME_FIELDS
AGEING_NON_TIME_FIELDS = _NON_TIME_FIELDS

_P = TypeVar("_P", bound=Payoff)


def age_payoff(payoff: _P, dt: float) -> _P:
    """`payoff` with every time field shifted by `-dt` (one model step closer to expiry).

    Rebuilding through `dataclasses.replace` re-runs the term sheet's own validation, so a
    product with an observation at `t <= dt` (which would be dated at or before the aged
    valuation date) raises here -- the load-time dry run turns that into a load error.
    Raises `ValueError` for a class missing from the ageing table.
    """
    time_fields = _TIME_FIELDS.get(type(payoff))
    if time_fields is None:
        raise ValueError(
            f"{type(payoff).__name__} is not in the ageing table: theta needs every time field "
            "of the term sheet shifted (ADR-006 Amendment 9 s2)"
        )
    changes: dict[str, object] = {}
    for name in time_fields:
        value = getattr(payoff, name)
        if value is None:
            continue
        changes[name] = (
            tuple(t - dt for t in value) if isinstance(value, tuple) else float(value) - dt
        )
    return cast(_P, dataclasses.replace(cast(Any, payoff), **changes))


def _with(params: HestonParams, **changes: float) -> HestonParams:
    """A validated copy (so `allow_inf_nan=False` and the positivity bounds still apply)."""
    return HestonParams.model_validate({**params.model_dump(), **changes})


def compute_greeks(
    payoff: _P,
    params: HestonParams,
    *,
    scheme: Scheme,
    n_steps_per_year: int,
    n_paths: int,
    seed: int,
    bumps: BumpConfig,
) -> Greeks:
    """CRN bump-and-revalue of one product at `params` (whose `s0` is the mark). See module doc.

    `n_steps = round(payoff.expiry * n_steps_per_year)` must be >= 2 (theta needs a step to drop).
    Ten sequential `simulate` calls; peak memory is ONE leg's footprint plus a one-column pairing
    stub of the base bundle, which is why the measured `GREEKS_PEAK_ARRAY_RATIO` is close to a
    single `simulate()`'s transient peak (`models/memory.py`).
    """
    n_steps = round(payoff.expiry * n_steps_per_year)
    if n_steps < 2:
        raise ValueError(
            f"n_steps = round({payoff.expiry} * {n_steps_per_year}) = {n_steps} must be >= 2"
        )
    sqrt_v0, sqrt_theta = math.sqrt(params.v0), math.sqrt(params.theta)
    h_sigma = bumps.bump_vol_abs
    if h_sigma >= min(sqrt_v0, sqrt_theta):
        raise ValueError(
            f"bump_vol_abs={h_sigma} must be < min(sqrt(v0), sqrt(theta)) = "
            f"{min(sqrt_v0, sqrt_theta)}: a larger bump drives the bumped variance non-positive"
        )
    dt = payoff.expiry / n_steps
    aged = age_payoff(payoff, dt)
    rng = PseudoRandomSource(seed, antithetic=True)

    def leg(
        p: HestonParams, pay: Payoff, source: RandomSource, steps: int, expiry: float
    ) -> tuple[PathBundle, NDArray[np.float64]]:
        engine = EngineConfig(
            scheme=scheme, n_steps=steps, n_paths=n_paths, expiry=expiry, antithetic=True
        )
        bundle = simulate(p, engine, source)
        return bundle, discount(pay.cashflows(bundle, r=p.r), r=p.r)

    def vec(p: HestonParams) -> NDArray[np.float64]:
        # Only the discounted vector survives the call: the bundle is released immediately.
        return leg(p, payoff, rng, n_steps, payoff.expiry)[1]

    base, d0 = leg(params, payoff, rng, n_steps, payoff.expiry)
    # mc_estimate needs only the pairing structure (n_pairs, antithetic, low_discrepancy) and the
    # leading dimension, not the (n_paths, n_steps + 1) S and v matrices: keep a one-column stub.
    pairing = dataclasses.replace(base, S=base.S[:, :1].copy(), v=base.v[:, :1].copy())
    del base

    h_s = bumps.bump_spot_rel * params.s0
    d_su = vec(_with(params, s0=params.s0 + h_s))
    d_sd = vec(_with(params, s0=params.s0 - h_s))
    d_vu = vec(_with(params, v0=(sqrt_v0 + h_sigma) ** 2, theta=(sqrt_theta + h_sigma) ** 2))
    d_vd = vec(_with(params, v0=(sqrt_v0 - h_sigma) ** 2, theta=(sqrt_theta - h_sigma) ** 2))
    d_ru = vec(_with(params, r=params.r + bumps.bump_rate_abs))
    d_rd = vec(_with(params, r=params.r - bumps.bump_rate_abs))
    d_qu = vec(_with(params, q=params.q + bumps.bump_div_abs))
    d_qd = vec(_with(params, q=params.q - bumps.bump_div_abs))
    _, d_aged = leg(params, aged, DropLeadingStepsSource(rng, 1), n_steps - 1, payoff.expiry - dt)

    def est(per_path: NDArray[np.float64]) -> Estimate:
        r = mc_estimate(pairing, per_path)
        return Estimate(value=r.pv, std_err=r.std_err)

    return Greeks(
        pv=est(d0),
        delta=est((d_su - d_sd) / (2.0 * h_s)),
        gamma=est((d_su - 2.0 * d0 + d_sd) / h_s**2),
        vega=est((d_vu - d_vd) / (2.0 * h_sigma)),
        rho=est((d_ru - d_rd) / (2.0 * bumps.bump_rate_abs)),
        dq=est((d_qu - d_qd) / (2.0 * bumps.bump_div_abs)),
        theta=est((d_aged - d0) / (dt * bumps.theta_days_per_year)),
    )
