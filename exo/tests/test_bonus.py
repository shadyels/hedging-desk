"""Tests for exo.products.bonus.BonusCertificate: hand-built ledgers and term-sheet validation
(the blocking gates are G-BON/G-BON-par in test_validation_gates_equity.py)."""

from __future__ import annotations

import numpy as np
import pytest
from _equity_helpers import forced_bundle

from exo.products.base import Monitoring
from exo.products.bonus import BonusCertificate


def _bonus(**overrides: object) -> BonusCertificate:
    kwargs: dict[str, object] = {
        "underlying": "SYN",
        "ratio": 0.5,
        "bonus_level": 115.0,
        "barrier": 75.0,
        "expiry": 1.0,
        "monitoring": Monitoring.DISCRETE,
        "observations": (0.5, 1.0),
    }
    kwargs.update(overrides)
    return BonusCertificate(**kwargs)


def test_bonus_floor_for_survivors_and_plain_underlying_once_knocked() -> None:
    # grid t = [0, .5, 1]. path 0: survives, ends 100 -> bonus pays 115. path 1: survives, ends
    # 130 -> underlying. path 2: touches 70 (knocked), ends 100 -> plain underlying, no bonus.
    bundle = forced_bundle([[100.0, 90.0, 100.0], [100.0, 110.0, 130.0], [100.0, 70.0, 100.0]])
    ledger = _bonus().cashflows(bundle)
    np.testing.assert_array_equal(ledger.t, [1.0])
    np.testing.assert_allclose(ledger.amounts[:, 0], [0.5 * 115.0, 0.5 * 130.0, 0.5 * 100.0])


@pytest.mark.parametrize("field", ["ratio", "bonus_level", "barrier", "expiry"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0.0, -1.0])
def test_rejects_bad_scalar_fields(field: str, bad: float) -> None:
    with pytest.raises(ValueError):
        _bonus(**{field: bad})


@pytest.mark.parametrize("barrier", [115.0, 130.0])
def test_rejects_barrier_not_below_bonus_level(barrier: float) -> None:
    with pytest.raises(ValueError):
        _bonus(barrier=barrier)


def test_monitoring_typo_and_observations_iff_discrete() -> None:
    with pytest.raises(ValueError):
        _bonus(monitoring="Discrete")
    with pytest.raises(ValueError):
        _bonus(observations=None)  # DISCRETE needs observations
    with pytest.raises(ValueError):
        _bonus(monitoring=Monitoring.CONTINUOUS_BRIDGE)  # observations given for CONTINUOUS
    with pytest.raises(ValueError):
        _bonus(observations=(0.5, 1.5))  # outside (0, expiry]
