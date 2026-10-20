"""Deterministic unit tests for `exo.products.mini_future.MiniFuture` on hand-built bundles.

`forced_bundle` (see `_equity_helpers.py`) gives a 5-column grid `t = 0, .25, .5, .75, 1` with a
constant variance; `v=0.0` makes every bridge `p_cross` exactly 0 (no diffusion inside a step),
so each stop-out below is decided by the step's ENDPOINTS alone and the ledger can be written
down by hand. Column `c` of the ledger is dated `t[c + 1]`.
"""

from __future__ import annotations

import math
import tracemalloc
from typing import Literal

import numpy as np
import pytest
from _equity_helpers import forced_bundle

from exo.models.heston import simulate
from exo.models.params import EngineConfig, HestonParams
from exo.models.rng import PseudoRandomSource
from exo.products import mini_future
from exo.products.mini_future import MiniFuture

_RATIO = 0.1


def _mf(
    side: Literal["long", "short"] = "long",
    *,
    financing_level: float | None = None,
    spread: float = 0.0,
    stop: float | None = None,
    buffer: float = 0.05,
    reset_times: tuple[float, ...] = (),
    horizon: float = 1.0,
) -> MiniFuture:
    long = side == "long"
    return MiniFuture(
        underlying="SYN",
        side=side,
        ratio=_RATIO,
        financing_level=financing_level
        if financing_level is not None
        else (80.0 if long else 120.0),
        financing_spread=spread,
        stop_loss_level=stop if stop is not None else (85.0 if long else 115.0),
        stop_loss_buffer=buffer,
        reset_times=reset_times,
        horizon=horizon,
    )


def test_expiry_is_the_carry_horizon() -> None:
    assert _mf(horizon=0.75).expiry == 0.75


def test_survivor_pays_spot_minus_accrued_financing_at_horizon() -> None:
    """Long, r=0.1, spread 0.05 -> rate_F = 0.15; short: rate_F = r - spread = 0.05. The long
    stop is 95 so it stays beyond F_T = 80 * exp(0.15) = 92.9 (the accrual guard)."""
    bundle = forced_bundle([[100.0, 101.0, 102.0, 103.0, 104.0]], v=0.0)
    ledger = _mf(spread=0.05, stop=95.0).cashflows(bundle, r=0.1)
    np.testing.assert_allclose(ledger.t, [0.25, 0.5, 0.75, 1.0])
    expected = _RATIO * (104.0 - 80.0 * math.exp(0.15))
    np.testing.assert_allclose(ledger.amounts, [[0.0, 0.0, 0.0, expected]], atol=1e-12)

    bundle = forced_bundle([[100.0, 99.0, 98.0, 97.0, 96.0]], v=0.0)
    ledger = _mf("short", spread=0.05).cashflows(bundle, r=0.1)
    expected = -_RATIO * (96.0 - 120.0 * math.exp(0.05))
    np.testing.assert_allclose(ledger.amounts, [[0.0, 0.0, 0.0, expected]], atol=1e-12)


def test_in_step_stop_pays_level_minus_financing_at_step_end_column() -> None:
    """Path falls through the stop between t1 and t2: residual `L - F_2` lands in column 1
    (dated t2), nothing afterwards. r=0.1, spread 0 -> F_2 = 80 * exp(0.1 * 0.5). The stop is 90
    so it stays beyond F_T = 80 * exp(0.1) = 88.4 (the accrual guard)."""
    bundle = forced_bundle([[100.0, 100.0, 80.0, 70.0, 60.0]], v=0.0)
    ledger = _mf(stop=90.0).cashflows(bundle, r=0.1)
    expected = _RATIO * (90.0 - 80.0 * math.exp(0.05))
    np.testing.assert_allclose(ledger.amounts, [[0.0, expected, 0.0, 0.0]], atol=1e-12)


def test_short_in_step_stop_has_flipped_sign() -> None:
    bundle = forced_bundle([[100.0, 100.0, 118.0, 125.0, 130.0]], v=0.0)
    ledger = _mf("short").cashflows(bundle, r=0.0)
    # stopped at L=115 inside step 1; residual sg * (L - F) = -(115 - 120) = +5
    np.testing.assert_allclose(ledger.amounts, [[0.0, _RATIO * 5.0, 0.0, 0.0]], atol=1e-12)


def test_reset_jump_pays_spot_minus_financing_at_the_previous_column() -> None:
    """spread 0.1, r=0, buffer 0.1: F_2 = 80 * exp(0.05) = 84.10, so the level reset at t=0.5 is
    F_2 * 1.1 = 92.51 -- computed from the ACCRUED financing level, not F0 * 1.1 = 88. Path A
    (S_2 = 90) is live against the old level 85 and against 88, but already beyond 92.51 -> a
    reset jump paying `S_2 - F_2` (+5.90: positive, since the path was alive against a level
    beyond F) at t_2 = column 1. Path B (S_2 = 95) is live against both and survives to pay
    `S_4 - F_4` at the horizon. A level mistakenly rebuilt from F0 would let path A survive,
    which it does not. (F stays below the level in force on every step, the accrual guard.)"""
    bundle = forced_bundle(
        [
            [100.0, 100.0, 90.0, 90.0, 90.0],
            [100.0, 100.0, 95.0, 110.0, 140.0],
        ],
        v=0.0,
    )
    ledger = _mf(spread=0.1, buffer=0.1, reset_times=(0.5,)).cashflows(bundle, r=0.0)
    f2 = 80.0 * math.exp(0.05)
    f4 = 80.0 * math.exp(0.1)
    # fixture sanity: path A is live against an F0-based level (88) but not the accrued one
    assert 88.0 < 90.0 < f2 * 1.1 < 95.0 and f2 * 1.1 > f4
    np.testing.assert_allclose(
        ledger.amounts,
        [
            [0.0, _RATIO * (90.0 - f2), 0.0, 0.0],
            [0.0, 0.0, 0.0, _RATIO * (140.0 - f4)],
        ],
        atol=1e-12,
    )


def test_levels_are_recomputed_at_every_reset() -> None:
    """Resets at t=0.25 and t=0.75, buffer 0.1, r=0.05: the level in force is 85 on step 0,
    `1.1 * F_1` on steps 1-2 and `1.1 * F_3` on step 3. Path A (S_2 = 88) is live against 85 but
    breaches the first reset level (89.1) -> in-step stop in step 1, residual `L_1 - F_2` in
    column 1. Path B (S_4 = 90) is live against the first reset level but breaches the SECOND
    (91.4) -> in-step stop in step 3, residual `L_3 - F_4` in column 3."""
    bundle = forced_bundle(
        [
            [100.0, 100.0, 88.0, 100.0, 100.0],
            [100.0, 100.0, 100.0, 100.0, 90.0],
        ],
        v=0.0,
    )
    ledger = _mf(reset_times=(0.25, 0.75), buffer=0.1).cashflows(bundle, r=0.05)

    def fin(t: float) -> float:
        return 80.0 * math.exp(0.05 * t)

    level1, level3 = 1.1 * fin(0.25), 1.1 * fin(0.75)
    assert 88.0 < level1 < 90.0 < level3  # fixture sanity
    np.testing.assert_allclose(
        ledger.amounts,
        [
            [0.0, _RATIO * (level1 - fin(0.5)), 0.0, 0.0],
            [0.0, 0.0, 0.0, _RATIO * (level3 - fin(1.0))],
        ],
        atol=1e-12,
    )


@pytest.mark.parametrize(("side", "spread"), [("long", 0.05), ("short", 0.1)])
def test_stop_loss_overtaken_by_accruing_financing_raises_unless_reset(
    side: Literal["long", "short"], spread: float
) -> None:
    """Code review F1. r=0.0425: long rate_F = +0.0925 carries F from 80 to 87.7 by t=1, past the
    fixed stop 85; short rate_F = -0.0575 carries F from 120 to 113.3, past the stop 115. Each
    knock-out would pay `sg * (L - F) < 0`, a silently wrong PV, so it must raise. The same
    sheet WITH resets re-bases the level on the accrued F and prices."""
    bundle = forced_bundle([[100.0, 100.0, 100.0, 100.0, 100.0]], v=0.0)
    with pytest.raises(ValueError, match="not beyond the accrued financing level"):
        _mf(side, spread=spread).cashflows(bundle, r=0.0425)
    ledger = _mf(side, spread=spread, reset_times=(0.25, 0.5, 0.75)).cashflows(bundle, r=0.0425)
    assert np.all(ledger.amounts[:, :3] == 0.0)  # flat path at 100 never stops out


def test_reset_at_the_horizon_has_no_effect() -> None:
    bundle = forced_bundle([[100.0, 100.0, 100.0, 100.0, 90.0]], v=0.0)
    with_reset = _mf(reset_times=(1.0,)).cashflows(bundle, r=0.03)
    without = _mf().cashflows(bundle, r=0.03)
    np.testing.assert_array_equal(with_reset.amounts, without.amounts)


def test_ledger_is_bound_to_the_products_own_horizon() -> None:
    """A shared bundle may run past `horizon`: the ledger stops at the horizon and S_T is read
    there, not at the bundle's last column."""
    bundle = forced_bundle([[100.0, 101.0, 102.0, 50.0, 10.0]], v=0.0)
    ledger = _mf(horizon=0.5).cashflows(bundle, r=0.0)
    np.testing.assert_allclose(ledger.t, [0.25, 0.5])
    np.testing.assert_allclose(ledger.amounts, [[0.0, _RATIO * (102.0 - 80.0)]], atol=1e-12)


def test_zero_variance_steps_have_no_bridge_crossing() -> None:
    """v=0: two LIVE endpoints cannot hide a crossing, so a path hugging (but not touching) the
    stop survives with probability exactly 1; with v>0 the same path has a positive stop-out
    probability and so a strictly different ledger."""
    path = [[100.0, 86.0, 86.0, 86.0, 86.0]]
    flat = _mf().cashflows(forced_bundle(path, v=0.0), r=0.0)
    np.testing.assert_allclose(flat.amounts, [[0.0, 0.0, 0.0, _RATIO * 6.0]], atol=1e-12)
    noisy = _mf().cashflows(forced_bundle(path, v=0.04), r=0.0)
    assert noisy.amounts[0, 3] < flat.amounts[0, 3]
    assert np.all(noisy.amounts[0, :3] > 0.0)


@pytest.mark.parametrize(("side", "stop"), [("long", 100.0), ("short", 100.0)])
def test_inception_breach_raises(side: Literal["long", "short"], stop: float) -> None:
    bundle = forced_bundle([[100.0, 100.0, 100.0, 100.0, 100.0]])
    with pytest.raises(ValueError, match="already breached"):
        _mf(side, stop=stop).cashflows(bundle, r=0.0)


@pytest.mark.parametrize("bad_r", [float("nan"), float("inf")])
def test_cashflows_rejects_non_finite_r(bad_r: float) -> None:
    bundle = forced_bundle([[100.0, 100.0, 100.0, 100.0, 100.0]])
    with pytest.raises(ValueError, match="finite"):
        _mf().cashflows(bundle, r=bad_r)


def test_r_is_keyword_only_and_required() -> None:
    bundle = forced_bundle([[100.0, 100.0, 100.0, 100.0, 100.0]])
    with pytest.raises(TypeError):
        _mf().cashflows(bundle)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        _mf().cashflows(bundle, 0.0)  # type: ignore[misc]


@pytest.mark.parametrize("field", ["ratio", "financing_level", "stop_loss_level", "horizon"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0.0, -1.0])
def test_rejects_non_positive_or_non_finite_positive_fields(field: str, bad: float) -> None:
    kwargs: dict[str, object] = dict(
        underlying="SYN",
        side="long",
        ratio=0.1,
        financing_level=80.0,
        financing_spread=0.0,
        stop_loss_level=85.0,
        stop_loss_buffer=0.05,
        reset_times=(),
        horizon=1.0,
    )
    kwargs[field] = bad
    with pytest.raises(ValueError):
        MiniFuture(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_rejects_non_finite_spread(bad: float) -> None:
    with pytest.raises(ValueError, match="financing_spread"):
        _mf(spread=bad)


def test_negative_spread_is_allowed() -> None:
    _mf(spread=-0.02)


@pytest.mark.parametrize("buffer", [0.0, 1.0, -0.1, 1.5, float("nan"), float("inf")])
def test_rejects_buffer_outside_open_unit_interval(buffer: float) -> None:
    with pytest.raises(ValueError, match="stop_loss_buffer"):
        _mf(buffer=buffer)


def test_rejects_side_typo() -> None:
    with pytest.raises(ValueError, match="side"):
        _mf("Long")  # type: ignore[arg-type]


def test_long_needs_stop_above_financing_and_short_below() -> None:
    with pytest.raises(ValueError, match="long"):
        _mf("long", financing_level=80.0, stop=80.0)
    with pytest.raises(ValueError, match="long"):
        _mf("long", financing_level=80.0, stop=70.0)
    with pytest.raises(ValueError, match="short"):
        _mf("short", financing_level=120.0, stop=120.0)
    with pytest.raises(ValueError, match="short"):
        _mf("short", financing_level=120.0, stop=130.0)


@pytest.mark.parametrize(
    "reset_times", [(0.0,), (1.5,), (0.5, 0.25), (0.5, 0.5), (float("nan"),), (float("inf"),)]
)
def test_rejects_invalid_reset_schedule(reset_times: tuple[float, ...]) -> None:
    with pytest.raises(ValueError):
        _mf(reset_times=reset_times)


def test_reset_off_the_grid_raises() -> None:
    bundle = forced_bundle([[100.0, 100.0, 100.0, 100.0, 100.0]])
    with pytest.raises(ValueError):
        _mf(reset_times=(0.3,)).cashflows(bundle, r=0.0)


def test_mini_future_peak_memory_covers_measured_tracemalloc_ratio() -> None:
    """Measures the REAL `MiniFuture.cashflows` call (never re-derives the ratio by counting
    arrays -- this repo's hand-counted memory constants were wrong three times, always low).
    Warm-up call OUTSIDE the tracemalloc window so one-time lazy allocations do not inflate the
    peak. Measured at n_paths=20_000, n_steps=252: peak/array = 8.22 (array = one
    `(n_paths, n_steps + 1)` float64), the same to 3 digits at 40_000 x 126 and with 12 resets.

    Asserts `measured <= ceiling` AND `ceiling <= measured * 1.06`, so the declared constant can
    neither go stale-low (a regression the ceiling no longer covers) nor stale-loose.
    """
    params = HestonParams(
        s0=100.0, r=0.0425, q=0.0, v0=0.04, kappa=1.5, theta=0.04, xi=1e-4, rho=0.0
    )
    note = _mf(spread=0.0, reset_times=tuple(0.25 * k for k in range(1, 4)))
    n_paths, n_steps = 20_000, 252
    engine = EngineConfig(
        scheme="qe", n_steps=n_steps, n_paths=n_paths, expiry=1.0, antithetic=True
    )
    bundle = simulate(params, engine, PseudoRandomSource(seed=1))
    note.cashflows(bundle, r=0.0425)  # warm-up, outside the measured window

    tracemalloc.start()
    try:
        note.cashflows(bundle, r=0.0425)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    measured = peak / (n_paths * (n_steps + 1) * 8)
    ceiling = mini_future._MINI_FUTURE_PEAK_ARRAY_RATIO
    assert measured <= ceiling, (
        f"n_paths={n_paths} n_steps={n_steps}: measured peak/array {measured:.3f} exceeds "
        f"the declared ceiling {ceiling}"
    )
    assert ceiling <= measured * 1.06, (
        f"n_paths={n_paths} n_steps={n_steps}: ceiling {ceiling} is more than 6% above the "
        f"measured {measured:.3f} -- re-measure and tighten it"
    )
