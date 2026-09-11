"""Tests for exo.models.analytic.bs_barrier_price: the three identities from the brief
(P2.M1 slice 2, s4.5) rather than copied magic numbers -- a barrier closed form is easy to
get subtly sign-wrong, and these identities catch that independent of any specific reference
value:

- down-and-out + down-and-in == vanilla (and the up- equivalent), to machine precision;
- H -> 0 (down barrier far below spot) gives the vanilla;
- H -> S0 drives the knock-out to ~0.
"""

from __future__ import annotations

from exo.models.analytic import bs_barrier_price, bs_call_price, bs_put_price

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
