"""Unit tests for exo.products.tarf: Tarf / Ptarf and the shared target-redemption state loop."""

from __future__ import annotations

import itertools
import math
import tomllib
from pathlib import Path

import numpy as np
import pytest
from _equity_helpers import forced_bundle
from numpy.testing import assert_allclose, assert_array_equal

from exo.models.heston import simulate
from exo.models.params import EngineConfig, load_models_section
from exo.models.rng import PseudoRandomSource
from exo.products.base import ScheduleAlignmentError
from exo.products.pricer import discount
from exo.products.tarf import Ptarf, Tarf

_EURUSD = load_models_section(
    tomllib.loads((Path(__file__).parents[1] / "exo.toml").read_text())["models"]
)[1]["EURUSD"]


def _tarf(
    n: int,
    *,
    notional: float = 1.0,
    strike: float = 1.0,
    leverage: float = 2.0,
    target: float = 1.0,
) -> Tarf:
    return Tarf(
        "EURUSD", notional, strike, leverage, target, tuple(i / 12 for i in range(1, n + 1)), n / 12
    )


def test_u1_capped_partial_last_fixing() -> None:
    # Column 0 is the t=0 state (S0); fixing columns are 1..4 on a 1/12 grid.
    bundle = forced_bundle([[1.08, 1.10, 1.07, 1.11, 1.12]], expiry=4 / 12)
    tarf = Tarf("EURUSD", 1e6, 1.08, 2.0, 40_000.0, (1 / 12, 2 / 12, 3 / 12, 4 / 12), 4 / 12)
    ledger = tarf.cashflows(bundle, r=0.0425)
    assert_allclose(ledger.amounts[0], [20000, -20000, 20000, 0], rtol=1e-9, atol=1e-9)
    assert_allclose(discount(ledger, r=0.0425), [19859.082984], rtol=1e-8)


def test_u2_ptarf_worked_example() -> None:
    bundle = forced_bundle([[1.08, 1.07, 1.10, 1.04, 1.13, 1.09]], expiry=5 / 12)
    ptarf = Ptarf(
        "EURUSD", 1e6, 1.05, 1.08, 1.11, 2.0, 40_000.0, tuple(i / 12 for i in range(1, 6)), 5 / 12
    )
    ledger = ptarf.cashflows(bundle, r=0.0425)
    assert_allclose(ledger.amounts[0], [20000, 10000, -20000, -40000, 10000], rtol=1e-9)
    assert_allclose(discount(ledger, r=0.0425), [-19542.768979], rtol=1e-8)


def test_u3_exact_equality_breach() -> None:
    # Fixing 2 lands acc on the target exactly (0.5 + 0.5 == 1.0): `>=` knocks out there; a `>`
    # rule would stay alive and charge the fixing-3 loss (2 * 0.5 = -1.0).
    bundle = forced_bundle([[1.0, 1.5, 1.5, 0.5]], expiry=3 / 12)
    ledger = _tarf(3).cashflows(bundle, r=0.0)
    assert_array_equal(ledger.amounts, [[0.5, 0.5, 0.0]])


def test_u4_accumulation_never_exceeds_target() -> None:
    engine = EngineConfig(scheme="qe", n_steps=48, n_paths=2000, expiry=1.0, antithetic=True)
    bundle = simulate(_EURUSD, engine, PseudoRandomSource(seed=7))
    g = 50_000.0
    tarf = Tarf("EURUSD", 1e6, 1.08, 2.0, g, tuple(i / 12 for i in range(1, 13)), 1.0)
    amounts = tarf.cashflows(bundle, r=0.0425).amounts
    gains = np.maximum(amounts, 0.0)
    # amounts are gain - loss, and a fixing is either in the money or not, so max(c, 0) == paid
    assert np.all(gains.sum(axis=1) <= g * (1 + 1e-12))
    reached = np.isclose(gains.sum(axis=1), g, rtol=1e-12, atol=0.0)
    assert reached.any()
    for row, hit in zip(amounts, reached, strict=True):
        if hit:
            first = int(np.argmax(np.cumsum(np.maximum(row, 0.0)) >= g * (1 - 1e-12)))
            assert np.all(row[first + 1 :] == 0.0)


def test_u5_loss_never_knocks_out_nor_reduces_accumulator() -> None:
    bundle = forced_bundle([[1.0, 1.75, 1.0, 0.9, 1.5]], expiry=4 / 12)
    ledger = _tarf(4).cashflows(bundle, r=0.0)
    assert_allclose(ledger.amounts[0], [0.75, 0.0, -0.2, 0.25], rtol=1e-9, atol=1e-12)


@pytest.mark.parametrize("cls", [Tarf, Ptarf])
def test_u6_24_accumulated_fixings(cls: type) -> None:
    fixings = tuple(itertools.accumulate([1 / 12] * 24))
    assert abs(fixings[-1] - 2.0) < 1e-9
    kw = dict(
        underlying="EURUSD", notional=1.0, leverage=2.0, target=1.0, fixings=fixings, expiry=2.0
    )
    if cls is Tarf:
        cls(strike=1.08, **kw)
    else:
        cls(strike_low=1.05, pivot=1.08, strike_high=1.11, **kw)


_TARF_OK = dict(
    underlying="EURUSD",
    notional=1.0,
    strike=1.0,
    leverage=2.0,
    target=1.0,
    fixings=(1 / 12,),
    expiry=1 / 12,
)
_PTARF_OK = dict(
    underlying="EURUSD",
    notional=1.0,
    strike_low=1.0,
    pivot=1.1,
    strike_high=1.2,
    leverage=2.0,
    target=1.0,
    fixings=(1 / 12,),
    expiry=1 / 12,
)
_BAD = [math.nan, math.inf, 0.0, -1.0]


@pytest.mark.parametrize("bad", _BAD)
@pytest.mark.parametrize("field", ["notional", "strike", "leverage", "target", "expiry"])
def test_u7_tarf_rejects_bad_numeric(field: str, bad: float) -> None:
    with pytest.raises(ValueError):
        Tarf(**{**_TARF_OK, field: bad})  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", _BAD)
@pytest.mark.parametrize(
    "field", ["notional", "strike_low", "pivot", "strike_high", "leverage", "target", "expiry"]
)
def test_u7_ptarf_rejects_bad_numeric(field: str, bad: float) -> None:
    with pytest.raises(ValueError):
        Ptarf(**{**_PTARF_OK, field: bad})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("lo", "pv", "hi"), [(1.1, 1.1, 1.2), (1.2, 1.1, 1.3), (1.0, 1.2, 1.2), (1.0, 1.3, 1.2)]
)
def test_u7_ptarf_ordering(lo: float, pv: float, hi: float) -> None:
    with pytest.raises(ValueError, match="strike_low < pivot < strike_high"):
        Ptarf(**{**_PTARF_OK, "strike_low": lo, "pivot": pv, "strike_high": hi})  # type: ignore[arg-type]


def test_u7_misaligned_fixings_raise() -> None:
    engine = EngineConfig(scheme="qe", n_steps=50, n_paths=4, expiry=1.0, antithetic=False)
    bundle = simulate(_EURUSD, engine, PseudoRandomSource(seed=1, antithetic=False))
    tarf = Tarf("EURUSD", 1.0, 1.08, 2.0, 1.0, (1 / 12, 1.0), 1.0)
    with pytest.raises(ScheduleAlignmentError):
        tarf.cashflows(bundle, r=0.0)


def test_u8_r_independence() -> None:
    bundle = forced_bundle([[1.0, 1.75, 1.0, 0.9, 1.5]], expiry=4 / 12)
    tarf = _tarf(4)
    assert_array_equal(
        tarf.cashflows(bundle, r=0.0).amounts, tarf.cashflows(bundle, r=0.05).amounts
    )
    ptarf = Ptarf(
        "EURUSD", 1.0, 0.8, 1.0, 1.2, 2.0, 1.0, tuple(i / 12 for i in range(1, 5)), 4 / 12
    )
    assert_array_equal(
        ptarf.cashflows(bundle, r=0.0).amounts, ptarf.cashflows(bundle, r=0.05).amounts
    )


def test_u9_pivot_tie_goes_to_upper_regime() -> None:
    bundle = forced_bundle([[1.08, 1.08]], expiry=1 / 12)
    ptarf = Ptarf("EURUSD", 1.0, 1.04, 1.08, 1.11, 1.0, 1e9, (1 / 12,), 1 / 12)
    amounts = ptarf.cashflows(bundle, r=0.0).amounts
    assert_allclose(amounts, [[0.03]], rtol=1e-9)  # upper: 1.11 - 1.08, not lower: 0.04
