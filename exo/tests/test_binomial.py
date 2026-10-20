"""Deterministic unit tests for `exo.models.binomial.crr_price` (the G-W3 Bermudan reference)."""

from __future__ import annotations

import pytest

from exo.models.analytic import bs_put_price
from exo.models.binomial import crr_price

_KW = {"s0": 100.0, "strike": 100.0, "r": 0.06, "q": 0.0, "sigma": 0.2, "expiry": 1.0}


def test_european_tree_matches_black_scholes() -> None:
    tree = crr_price(**_KW, option_type="put", n_tree_steps=10_000, exercise_every=None)
    assert abs(tree - bs_put_price(**_KW)) < 5e-4


def test_bermudan_on_the_lsm_dates_is_insensitive_to_tree_refinement() -> None:
    """50 exercise dates either way (200 or 201 tree steps per date)."""
    coarse = crr_price(**_KW, option_type="put", n_tree_steps=10_000, exercise_every=200)
    fine = crr_price(**_KW, option_type="put", n_tree_steps=10_050, exercise_every=201)
    assert abs(coarse - fine) < 1e-4


def test_bermudan_50_sits_between_european_and_american() -> None:
    european = crr_price(**_KW, option_type="put", n_tree_steps=10_000, exercise_every=None)
    bermudan = crr_price(**_KW, option_type="put", n_tree_steps=10_000, exercise_every=200)
    american = crr_price(**_KW, option_type="put", n_tree_steps=10_000, exercise_every=1)
    assert european < bermudan < american


def test_exercise_every_must_divide_the_tree() -> None:
    with pytest.raises(ValueError, match="exercise_every"):
        crr_price(**_KW, option_type="put", n_tree_steps=1000, exercise_every=300)


def test_arbitrage_violating_tree_raises() -> None:
    """r*dt >> sigma*sqrt(dt) pushes the up-probability above 1."""
    kw = {**_KW, "r": 0.9, "sigma": 0.01}
    with pytest.raises(ValueError, match="up-probability"):
        crr_price(**kw, option_type="put", n_tree_steps=10, exercise_every=None)


@pytest.mark.parametrize("field", ["s0", "strike", "sigma", "expiry", "r", "q"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_non_finite_inputs_raise(field: str, bad: float) -> None:
    with pytest.raises(ValueError):
        crr_price(**{**_KW, field: bad}, option_type="put", n_tree_steps=100, exercise_every=None)


def test_bad_option_type_raises() -> None:
    with pytest.raises(ValueError, match="option_type"):
        crr_price(**_KW, option_type="straddle", n_tree_steps=100, exercise_every=None)  # type: ignore[arg-type]
