"""Tests for exo.products.warrant.Warrant: intrinsic payoffs, the American guard, and
term-sheet validation (the blocking gate is G-W1 in test_validation_gates_equity.py)."""

from __future__ import annotations

import numpy as np
import pytest
from _equity_helpers import forced_bundle

from exo.products.warrant import Warrant


def _warrant(**overrides: object) -> Warrant:
    kwargs: dict[str, object] = {
        "underlying": "SYN",
        "option_type": "call",
        "strike": 100.0,
        "expiry": 1.0,
        "ratio": 0.1,
        "exercise": "european",
    }
    kwargs.update(overrides)
    return Warrant(**kwargs)


def test_call_and_put_intrinsic_scaled_by_ratio() -> None:
    s = np.array([80.0, 100.0, 130.0])
    np.testing.assert_allclose(_warrant().intrinsic(s), [0.0, 0.0, 3.0])
    np.testing.assert_allclose(_warrant(option_type="put").intrinsic(s), [2.0, 0.0, 0.0])


def test_european_ledger_reads_own_expiry_on_a_longer_bundle() -> None:
    # grid t = [0, .5, 1, 1.5, 2]; the warrant expires at 1.0 -> index 2, not the last column
    bundle = forced_bundle([[100.0, 100.0, 130.0, 500.0, 900.0]], expiry=2.0)
    ledger = _warrant().cashflows(bundle)
    np.testing.assert_array_equal(ledger.t, [1.0])
    np.testing.assert_allclose(ledger.amounts, [[3.0]])


def test_american_cashflows_raise_pointing_to_price_american() -> None:
    bundle = forced_bundle([[100.0, 100.0, 130.0]])
    with pytest.raises(ValueError, match="price_american"):
        _warrant(exercise="american").cashflows(bundle)


@pytest.mark.parametrize("field", ["strike", "expiry", "ratio"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0.0, -1.0])
def test_rejects_bad_scalar_fields(field: str, bad: float) -> None:
    with pytest.raises(ValueError):
        _warrant(**{field: bad})


def test_rejects_categorical_typos() -> None:
    with pytest.raises(ValueError):
        _warrant(option_type="Call")
    with pytest.raises(ValueError):
        _warrant(exercise="European")
