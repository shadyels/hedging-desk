"""Tests for exo.products.reverse_convertible.ReverseConvertible: hand-built ledgers, shared
longer bundles, and term-sheet validation (the blocking gates are G-RC1/G-RC2/G-BRC/G-BRC-par
in test_validation_gates_equity.py)."""

from __future__ import annotations

import numpy as np
import pytest
from _equity_helpers import forced_bundle

from exo.products.base import Monitoring
from exo.products.reverse_convertible import ReverseConvertible

_COUPONS = (0.5, 1.0)


def _rc(**overrides: object) -> ReverseConvertible:
    kwargs: dict[str, object] = {
        "underlying": "SYN",
        "notional": 1000.0,
        "strike": 100.0,
        "coupon_rate": 0.03,
        "coupon_times": _COUPONS,
        "expiry": 1.0,
    }
    kwargs.update(overrides)
    return ReverseConvertible(**kwargs)


def test_rc_ledger_columns_and_terminal_conversion() -> None:
    # grid t = [0, .5, 1]; path 0 ends above strike (full notional), path 1 at 60 (converts)
    bundle = forced_bundle([[100.0, 100.0, 120.0], [100.0, 90.0, 60.0]])
    ledger = _rc().cashflows(bundle, r=0.0)
    np.testing.assert_array_equal(ledger.t, [0.5, 1.0])
    np.testing.assert_allclose(
        ledger.amounts, [[30.0, 30.0 + 1000.0], [30.0, 30.0 + 600.0]], rtol=1e-12
    )


def test_brc_knocked_path_converts_and_surviving_path_is_par() -> None:
    # DISCRETE obs at 0.5, 1.0; barrier 80. path 0 dips to 70 at t=0.5 (knocked), ends 60.
    # path 1 never touches 80 and ends at 85 < K: survivor, par rather than converted.
    bundle = forced_bundle([[100.0, 70.0, 60.0], [100.0, 90.0, 85.0]])
    note = _rc(barrier=80.0, monitoring=Monitoring.DISCRETE, observations=(0.5, 1.0))
    ledger = note.cashflows(bundle, r=0.0)
    np.testing.assert_allclose(ledger.amounts[:, -1], [30.0 + 600.0, 30.0 + 1000.0], rtol=1e-12)


def test_terminal_leg_reads_own_expiry_on_a_longer_bundle() -> None:
    # bundle runs to t=2 (grid 0, .5, 1, 1.5, 2); the note expires at 1.0 -> index 2
    bundle = forced_bundle([[100.0, 100.0, 50.0, 500.0, 500.0]], expiry=2.0)
    ledger = _rc().cashflows(bundle, r=0.0)
    np.testing.assert_allclose(ledger.amounts[0], [30.0, 30.0 + 500.0], rtol=1e-12)


@pytest.mark.parametrize("field", ["notional", "strike", "coupon_rate", "expiry"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_rejects_non_finite_scalar_fields(field: str, bad: float) -> None:
    with pytest.raises(ValueError):
        _rc(**{field: bad})


@pytest.mark.parametrize(
    ("field", "bad"),
    [("notional", 0.0), ("notional", -1.0), ("strike", 0.0), ("coupon_rate", -0.01)],
)
def test_rejects_out_of_range_scalar_fields(field: str, bad: float) -> None:
    with pytest.raises(ValueError):
        _rc(**{field: bad})


def test_zero_coupon_rate_is_allowed() -> None:
    _rc(coupon_rate=0.0)


@pytest.mark.parametrize("coupon_times", [(), (0.5,), (0.5, 0.5, 1.0), (float("nan"), 1.0)])
def test_rejects_bad_coupon_schedule(coupon_times: tuple[float, ...]) -> None:
    with pytest.raises(ValueError):
        _rc(coupon_times=coupon_times)


@pytest.mark.parametrize("barrier", [float("nan"), float("inf"), 0.0, -5.0, 100.0, 120.0])
def test_rejects_bad_barrier(barrier: float) -> None:
    with pytest.raises(ValueError):
        _rc(barrier=barrier, monitoring=Monitoring.CONTINUOUS_BRIDGE)


def test_monitoring_required_iff_barrier_set() -> None:
    with pytest.raises(ValueError):
        _rc(barrier=80.0)  # barrier without monitoring
    with pytest.raises(ValueError):
        _rc(monitoring=Monitoring.CONTINUOUS_BRIDGE)  # monitoring without barrier
    with pytest.raises(ValueError):
        _rc(barrier=80.0, monitoring="continuous_bridgee")  # typo


def test_observations_iff_discrete() -> None:
    with pytest.raises(ValueError):
        _rc(barrier=80.0, monitoring=Monitoring.DISCRETE)  # DISCRETE without observations
    with pytest.raises(ValueError):
        _rc(barrier=80.0, monitoring=Monitoring.CONTINUOUS_BRIDGE, observations=(0.5, 1.0))
    with pytest.raises(ValueError):
        _rc(barrier=80.0, monitoring=Monitoring.DISCRETE, observations=(0.5, 2.0))
    with pytest.raises(ValueError):
        _rc(observations=(0.5, 1.0))  # observations without barrier
