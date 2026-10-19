"""Tests for exo.models.analytic.bs_barrier_price: the three identities from the brief
(P2.M1 slice 2, s4.5) rather than copied magic numbers -- a barrier closed form is easy to
get subtly sign-wrong, and these identities catch that independent of any specific reference
value:

- down-and-out + down-and-in == vanilla (and the up- equivalent), to machine precision;
- H -> 0 (down barrier far below spot) gives the vanilla;
- H -> S0 drives the knock-out to ~0.

Fix round 1 (MEDIUM-1, code review) added seam-continuity and single-branch-pinning tests: the
four in-out parity tests above prove nothing about the knock-IN formula itself (`out := vanilla
- in` satisfies parity trivially for ANY `in`), and every one of the eight tests above lands on
only two of the underlying A/B/C/D table's eight (direction, option_type, strike-vs-barrier)
branches. The tests below close the other six without a single copied magic number.

Fix round 1 (HIGH-2, code review) also added inception-breach and (HIGH-3, security review)
finiteness/categorical validation tests.
"""

from __future__ import annotations

import math
from typing import Literal

import pytest
from scipy.special import ndtr  # type: ignore[import-untyped]

from exo.models.analytic import (
    bs_barrier_price,
    bs_barrier_rebate_at_hit,
    bs_call_price,
    bs_put_price,
)

_S0 = 100.0
_STRIKE = 100.0
_R = 0.02
_Q = 0.01
_SIGMA = 0.2
_EXPIRY = 1.0


def _kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(s0=_S0, strike=_STRIKE, r=_R, q=_Q, sigma=_SIGMA, expiry=_EXPIRY)
    base.update(overrides)
    return base


def test_down_in_out_parity_call() -> None:
    vanilla = bs_call_price(**_kwargs())
    out = bs_barrier_price(
        **_kwargs(barrier=90.0, direction="down", knock="out", option_type="call")
    )
    inp = bs_barrier_price(
        **_kwargs(barrier=90.0, direction="down", knock="in", option_type="call")
    )
    assert abs((out + inp) - vanilla) < 1e-9


def test_down_in_out_parity_put() -> None:
    vanilla = bs_put_price(**_kwargs())
    out = bs_barrier_price(
        **_kwargs(barrier=90.0, direction="down", knock="out", option_type="put")
    )
    inp = bs_barrier_price(**_kwargs(barrier=90.0, direction="down", knock="in", option_type="put"))
    assert abs((out + inp) - vanilla) < 1e-9


def test_up_in_out_parity_call() -> None:
    vanilla = bs_call_price(**_kwargs())
    out = bs_barrier_price(
        **_kwargs(barrier=110.0, direction="up", knock="out", option_type="call")
    )
    inp = bs_barrier_price(**_kwargs(barrier=110.0, direction="up", knock="in", option_type="call"))
    assert abs((out + inp) - vanilla) < 1e-9


def test_up_in_out_parity_put() -> None:
    vanilla = bs_put_price(**_kwargs())
    out = bs_barrier_price(**_kwargs(barrier=110.0, direction="up", knock="out", option_type="put"))
    inp = bs_barrier_price(**_kwargs(barrier=110.0, direction="up", knock="in", option_type="put"))
    assert abs((out + inp) - vanilla) < 1e-9


def test_down_out_call_converges_to_vanilla_as_barrier_to_zero() -> None:
    vanilla = bs_call_price(**_kwargs())
    out = bs_barrier_price(
        **_kwargs(barrier=1e-6, direction="down", knock="out", option_type="call")
    )
    assert abs(out - vanilla) < 1e-6


def test_down_out_call_knocks_out_to_near_zero_as_barrier_to_spot() -> None:
    out = bs_barrier_price(
        **_kwargs(barrier=_S0 - 1e-6, direction="down", knock="out", option_type="call")
    )
    assert out < 1e-3


def test_up_out_put_converges_to_vanilla_as_barrier_to_infinity() -> None:
    vanilla = bs_put_price(**_kwargs())
    out = bs_barrier_price(**_kwargs(barrier=1e6, direction="up", knock="out", option_type="put"))
    assert abs(out - vanilla) < 1e-6


def test_up_out_put_knocks_out_to_near_zero_as_barrier_to_spot() -> None:
    out = bs_barrier_price(
        **_kwargs(barrier=_S0 + 1e-6, direction="up", knock="out", option_type="put")
    )
    assert out < 1e-3


# --- HIGH-2: barrier already breached at inception must raise, not return a negative price ---


def test_down_call_raises_when_barrier_already_breached_at_inception() -> None:
    """Measured pre-fix (code review, fix round 1): S0=100, K=100, sigma=0.2, T=1, H=105 ->
    down-and-out call = -6.276812, not the true 0."""
    with pytest.raises(ValueError):
        bs_barrier_price(
            **_kwargs(barrier=105.0, direction="down", knock="out", option_type="call")
        )


def test_up_put_raises_when_barrier_already_breached_at_inception() -> None:
    with pytest.raises(ValueError):
        bs_barrier_price(**_kwargs(barrier=95.0, direction="up", knock="out", option_type="put"))


def test_down_call_raises_when_barrier_equals_spot_at_inception() -> None:
    with pytest.raises(ValueError):
        bs_barrier_price(**_kwargs(barrier=_S0, direction="down", knock="out", option_type="call"))


def test_up_call_raises_when_barrier_equals_spot_at_inception() -> None:
    with pytest.raises(ValueError):
        bs_barrier_price(**_kwargs(barrier=_S0, direction="up", knock="out", option_type="call"))


# --- MEDIUM-1: seam continuity across the strike-vs-barrier branch split, all 8 cells ---


@pytest.mark.parametrize(
    ("direction", "option_type", "s0", "barrier"),
    [
        ("down", "call", 150.0, 100.0),
        ("down", "put", 150.0, 100.0),
        ("up", "call", 50.0, 100.0),
        ("up", "put", 50.0, 100.0),
    ],
)
def test_knock_in_price_is_continuous_across_the_strike_equals_barrier_seam(
    direction: str, option_type: str, s0: float, barrier: float
) -> None:
    """`in_price` is defined by one branch when `strike > barrier` and a DIFFERENT branch
    otherwise; a correct closed form must agree at the seam to within numerical noise.
    Measured gaps (code review, fix round 1): ~6e-8."""
    eps = 1e-4
    below = bs_barrier_price(
        s0=s0,
        strike=barrier - eps,
        r=_R,
        q=_Q,
        sigma=_SIGMA,
        expiry=_EXPIRY,
        barrier=barrier,
        direction=direction,
        knock="in",
        option_type=option_type,
    )
    above = bs_barrier_price(
        s0=s0,
        strike=barrier + eps,
        r=_R,
        q=_Q,
        sigma=_SIGMA,
        expiry=_EXPIRY,
        barrier=barrier,
        direction=direction,
        knock="in",
        option_type=option_type,
    )
    assert abs(below - above) < 1e-4


# --- MEDIUM-1: pin the two branches ("A") no other test in this file's history reached ---


def test_up_call_with_strike_above_barrier_pins_branch_a_via_vanilla_identity() -> None:
    """Up-call, K > H, is branch `A` alone: algebraically identical to the vanilla call
    formula (`x1 == d1` in this parameterization). UI == vanilla and UO == 0 exactly."""
    s0, barrier, strike = 50.0, 100.0, 110.0
    vanilla = bs_call_price(s0=s0, strike=strike, r=_R, q=_Q, sigma=_SIGMA, expiry=_EXPIRY)
    ui = bs_barrier_price(
        s0=s0,
        strike=strike,
        r=_R,
        q=_Q,
        sigma=_SIGMA,
        expiry=_EXPIRY,
        barrier=barrier,
        direction="up",
        knock="in",
        option_type="call",
    )
    uo = bs_barrier_price(
        s0=s0,
        strike=strike,
        r=_R,
        q=_Q,
        sigma=_SIGMA,
        expiry=_EXPIRY,
        barrier=barrier,
        direction="up",
        knock="out",
        option_type="call",
    )
    assert abs(ui - vanilla) < 1e-9
    assert abs(uo - 0.0) < 1e-9


def test_down_put_with_strike_below_barrier_pins_branch_a_via_vanilla_identity() -> None:
    """Down-put, K < H, is branch `A` alone. DI == vanilla put and DO == 0 exactly."""
    s0, barrier, strike = 150.0, 100.0, 90.0
    vanilla = bs_put_price(s0=s0, strike=strike, r=_R, q=_Q, sigma=_SIGMA, expiry=_EXPIRY)
    di = bs_barrier_price(
        s0=s0,
        strike=strike,
        r=_R,
        q=_Q,
        sigma=_SIGMA,
        expiry=_EXPIRY,
        barrier=barrier,
        direction="down",
        knock="in",
        option_type="put",
    )
    do = bs_barrier_price(
        s0=s0,
        strike=strike,
        r=_R,
        q=_Q,
        sigma=_SIGMA,
        expiry=_EXPIRY,
        barrier=barrier,
        direction="down",
        knock="out",
        option_type="put",
    )
    assert abs(di - vanilla) < 1e-9
    assert abs(do - 0.0) < 1e-9


# --- HIGH-3 / MEDIUM-6: finiteness and categorical-membership guards ---


@pytest.mark.parametrize("field", ["s0", "strike", "barrier", "expiry", "sigma", "r", "q"])
def test_bs_barrier_price_rejects_non_finite_fields(field: str) -> None:
    kwargs = _kwargs(barrier=90.0, direction="down", knock="out", option_type="call")
    kwargs[field] = float("nan")
    with pytest.raises(ValueError):
        bs_barrier_price(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["option_type", "direction", "knock"])
def test_bs_barrier_price_rejects_case_typo_in_categorical_fields(field: str) -> None:
    kwargs = _kwargs(barrier=90.0, direction="down", knock="out", option_type="call")
    kwargs[field] = str(kwargs[field]).capitalize()
    with pytest.raises(ValueError):
        bs_barrier_price(**kwargs)  # type: ignore[arg-type]


# --- P2.M2 slice 2: bs_barrier_rebate_at_hit, pinned by optional-stopping identities ---


def _p_survive(
    *, s0: float, r: float, sigma: float, expiry: float, barrier: float, direction: str
) -> float:
    """Closed-form P(no hit before `expiry`) under GBM with q=0 -- independent of the rebate
    formula under test (reflection principle, not the Haug rebate term)."""
    nu = r - 0.5 * sigma**2
    x = math.log(barrier / s0)
    sd = sigma * math.sqrt(expiry)
    scale = (barrier / s0) ** (2.0 * nu / sigma**2)
    if direction == "down":
        return float(ndtr((-x + nu * expiry) / sd) - scale * ndtr((x + nu * expiry) / sd))
    return float(ndtr((x - nu * expiry) / sd) - scale * ndtr((-x - nu * expiry) / sd))


@pytest.mark.parametrize(
    ("direction", "r"),
    [("down", 0.0), ("up", 0.0), ("down", 0.02), ("up", 0.05)],
)
def test_rebate_at_hit_matches_optional_stopping_identity(
    direction: Literal["down", "up"], r: float
) -> None:
    """q=0 martingale identity: a long mini future with CONSTANT financing level F (F<B<S0,
    stop at the down barrier B, residual B-F paid at the hit) is worth
    `S0 - F * (e^{-rT} P_surv + rebate)`, and the survivors' leg is exactly a down-and-out call
    struck at F<B. Mirror image for the short (S0<B<F, up barrier, residual F-B):
    `F * (e^{-rT} P_surv + rebate) - S0` = up-and-out put + (F-B) * rebate. At r=0 the rebate
    IS P(hit) and both collapse to the bare `S0 - F` / `F - S0`. 4 cells, abs 1e-10 (measured
    <= 1.1e-14). This is what upgrades the down-call K<H and up-put K>H Reiner-Rubinstein cells
    from "self-referential" to pinned."""
    s0, sigma, expiry = 100.0, 0.2, 1.0
    if direction == "down":
        f, b = 80.0, 85.0
    else:
        f, b = 120.0, 115.0
    reb = bs_barrier_rebate_at_hit(
        s0=s0, r=r, q=0.0, sigma=sigma, expiry=expiry, barrier=b, direction=direction
    )
    surv = _p_survive(s0=s0, r=r, sigma=sigma, expiry=expiry, barrier=b, direction=direction)
    digital = math.exp(-r * expiry) * surv + reb

    knocked_out = bs_barrier_price(
        s0=s0,
        strike=f,
        r=r,
        q=0.0,
        sigma=sigma,
        expiry=expiry,
        barrier=b,
        direction=direction,
        knock="out",
        option_type="call" if direction == "down" else "put",
    )
    if direction == "down":
        lhs, rhs = knocked_out + (b - f) * reb, s0 - f * digital
    else:
        lhs, rhs = knocked_out + (f - b) * reb, f * digital - s0
    assert abs(lhs - rhs) <= 1e-10, f"lhs={lhs}, rhs={rhs}"


def test_rebate_at_hit_is_a_probability_like_quantity() -> None:
    """r=q=0 gives P(hit) in (0, 1); it grows as the barrier nears spot and with expiry."""
    kw = dict(s0=100.0, r=0.0, q=0.0, sigma=0.2, direction="down")
    far = bs_barrier_rebate_at_hit(**kw, expiry=1.0, barrier=70.0)  # type: ignore[arg-type]
    near = bs_barrier_rebate_at_hit(**kw, expiry=1.0, barrier=95.0)  # type: ignore[arg-type]
    longer = bs_barrier_rebate_at_hit(**kw, expiry=4.0, barrier=70.0)  # type: ignore[arg-type]
    assert 0.0 < far < near < 1.0
    assert far < longer < 1.0


def _rebate_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        s0=100.0, r=0.02, q=0.01, sigma=0.2, expiry=1.0, barrier=90.0, direction="down"
    )
    base.update(overrides)
    return base


@pytest.mark.parametrize("field", ["s0", "r", "q", "sigma", "expiry", "barrier"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_rebate_at_hit_rejects_non_finite_fields(field: str, bad: float) -> None:
    with pytest.raises(ValueError):
        bs_barrier_rebate_at_hit(**_rebate_kwargs(**{field: bad}))  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["s0", "sigma", "expiry", "barrier"])
def test_rebate_at_hit_rejects_non_positive_fields(field: str) -> None:
    with pytest.raises(ValueError):
        bs_barrier_rebate_at_hit(**_rebate_kwargs(**{field: 0.0}))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("direction", "barrier"), [("down", 100.0), ("down", 110.0), ("up", 100.0), ("up", 90.0)]
)
def test_rebate_at_hit_raises_on_inception_breach(direction: str, barrier: float) -> None:
    with pytest.raises(ValueError, match="already breached"):
        bs_barrier_rebate_at_hit(**_rebate_kwargs(direction=direction, barrier=barrier))  # type: ignore[arg-type]


def test_rebate_at_hit_rejects_direction_typo() -> None:
    with pytest.raises(ValueError):
        bs_barrier_rebate_at_hit(**_rebate_kwargs(direction="Down"))  # type: ignore[arg-type]


def test_rebate_at_hit_raises_on_negative_lambda_squared() -> None:
    """r=-0.04, q=-0.04 (b=0): mu^2 = 0.25 but 2r/sigma^2 = -2, so lambda^2 = -1.75 < 0."""
    with pytest.raises(ValueError, match="lambda"):
        bs_barrier_rebate_at_hit(**_rebate_kwargs(r=-0.04, q=-0.04))  # type: ignore[arg-type]
