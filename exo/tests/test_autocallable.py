"""Tests for exo.products.autocallable.Autocallable: the memory-coupon accumulator,
the zero-cashflow-after-call invariant, and memory>=no-memory pricing.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from exo.models.heston import simulate
from exo.models.params import EngineConfig, HestonParams
from exo.models.rng import PseudoRandomSource
from exo.products.autocallable import Autocallable
from exo.products.pricer import price_from_bundle

_MSFT = HestonParams(
    s0=420.00, r=0.0425, q=0.0080, v0=0.0500, kappa=2.00, theta=0.0500, xi=0.35, rho=-0.55
)


def _note(memory: bool) -> Autocallable:
    return Autocallable(
        underlying="MSFT",
        notional=1000.0,
        observations=(0.25, 0.5, 0.75, 1.0),
        autocall_trigger=460.0,
        coupon_barrier=400.0,
        coupon_rate=0.02,
        memory=memory,
        protection_barrier=350.0,
        initial_level=420.0,
        expiry=1.0,
    )


def test_memory_coupon_releases_exactly_the_missed_coupons() -> None:
    """Construct a bundle where S is BELOW coupon_barrier at obs 1 and 2 (missed) and
    ABOVE it at obs 3 (paid, never above autocall_trigger so no autocall) -- with
    memory=True the obs-3 cashflow must be notional*coupon_rate*(1+2) exactly."""
    note = Autocallable(
        underlying="SYNTH",
        notional=1000.0,
        observations=(0.25, 0.5, 0.75, 1.0),
        autocall_trigger=1e9,  # unreachable: never autocalls
        coupon_barrier=100.0,
        coupon_rate=0.02,
        memory=True,
        protection_barrier=100.0,
        initial_level=100.0,
        expiry=1.0,
    )
    n_steps = 4
    engine = EngineConfig(scheme="qe", n_steps=n_steps, n_paths=2, expiry=1.0, antithetic=False)
    bundle = simulate(
        HestonParams(s0=100.0, r=0.0, q=0.0, v0=0.04, kappa=1.5, theta=0.04, xi=0.6, rho=-0.7),
        engine,
        PseudoRandomSource(seed=1, antithetic=False),
    )
    # Force a synthetic S path (below, below, above, above the barrier) to test the
    # accumulator logic directly rather than relying on a random draw crossing it.
    forced = np.array(
        [
            [100.0, 90.0, 95.0, 150.0, 150.0],  # path 0: obs1 miss, obs2 miss, obs3 pay, obs4 pay
        ]
    )
    forced_bundle = replace(bundle, S=forced, v=np.abs(bundle.v[:1]))
    forced_ledger = note.cashflows(forced_bundle)

    # obs1: miss -> 0. obs2: miss -> 0. obs3: pay, missed=2 -> notional*rate*(1+2).
    # obs4: pay, missed=0 -> notional*rate*(1+0), PLUS maturity leg (never called,
    # S_T=150 >= protection_barrier=100 -> +notional).
    expected = np.array(
        [
            0.0,
            0.0,
            1000.0 * 0.02 * 3.0,
            1000.0 * 0.02 * 1.0 + 1000.0,
        ]
    )
    np.testing.assert_allclose(forced_ledger.amounts[0], expected, rtol=1e-12, atol=1e-9)


def test_called_path_emits_exactly_zero_after_call_index() -> None:
    note = Autocallable(
        underlying="SYNTH",
        notional=1000.0,
        observations=(0.25, 0.5, 0.75, 1.0),
        autocall_trigger=120.0,
        coupon_barrier=100.0,
        coupon_rate=0.02,
        memory=False,
        protection_barrier=100.0,
        initial_level=100.0,
        expiry=1.0,
    )
    n_steps = 4
    engine = EngineConfig(scheme="qe", n_steps=n_steps, n_paths=2, expiry=1.0, antithetic=False)
    bundle = simulate(
        HestonParams(s0=100.0, r=0.0, q=0.0, v0=0.04, kappa=1.5, theta=0.04, xi=0.6, rho=-0.7),
        engine,
        PseudoRandomSource(seed=1, antithetic=False),
    )
    # Grid t = [0, 0.25, 0.5, 0.75, 1.0]; obs dates (0.25, 0.5, 0.75, 1.0) land on grid
    # indices [1, 2, 3, 4], i.e. ledger column j corresponds to S[:, j + 1]. S=130 at
    # t=0.5 (ledger column 1) clears autocall_trigger=120, so the note calls there.
    forced = np.array([[100.0, 90.0, 130.0, 5.0, 5.0]])
    forced_bundle = replace(bundle, S=forced, v=np.abs(bundle.v[:1]))
    ledger = note.cashflows(forced_bundle)

    assert ledger.amounts[0, 1] > 0.0  # the call itself pays
    np.testing.assert_allclose(ledger.amounts[0, 2], 0.0, atol=0.0)  # exactly zero after
    np.testing.assert_allclose(ledger.amounts[0, 3], 0.0, atol=0.0)  # exactly zero after


def test_memory_prices_at_or_above_no_memory() -> None:
    engine = EngineConfig(scheme="qe", n_steps=48, n_paths=20_000, expiry=1.0, antithetic=True)
    bundle = simulate(_MSFT, engine, PseudoRandomSource(seed=41))

    memory_result = price_from_bundle(_note(memory=True), bundle, r=_MSFT.r)
    no_memory_result = price_from_bundle(_note(memory=False), bundle, r=_MSFT.r)

    assert memory_result.pv >= no_memory_result.pv


@pytest.mark.parametrize(
    "field",
    [
        "notional",
        "autocall_trigger",
        "coupon_barrier",
        "protection_barrier",
        "initial_level",
        "coupon_rate",
        "expiry",
    ],
)
def test_autocallable_rejects_non_finite_scalar_fields(field: str) -> None:
    """HIGH-3 (security review, fix round 1): before the fix, `Autocallable(notional=nan,
    ...)` constructed with no exception, then contaminated the ledger with NaN silently."""
    kwargs: dict[str, object] = dict(
        underlying="SYNTH",
        notional=1000.0,
        observations=(0.25, 0.5, 0.75, 1.0),
        autocall_trigger=120.0,
        coupon_barrier=100.0,
        coupon_rate=0.02,
        memory=False,
        protection_barrier=100.0,
        initial_level=100.0,
        expiry=1.0,
    )
    kwargs[field] = float("nan")
    with pytest.raises(ValueError):
        Autocallable(**kwargs)  # type: ignore[arg-type]


def test_autocallable_rejects_nan_observation() -> None:
    with pytest.raises(ValueError):
        Autocallable(
            underlying="SYNTH",
            notional=1000.0,
            observations=(0.25, float("nan"), 0.75, 1.0),
            autocall_trigger=120.0,
            coupon_barrier=100.0,
            coupon_rate=0.02,
            memory=False,
            protection_barrier=100.0,
            initial_level=100.0,
            expiry=1.0,
        )


def test_autocallable_accepts_terminal_observation_within_grid_tolerance() -> None:
    """LOW-1 (code review, fix round 1): `observations[-1] == expiry` must use the same
    `1e-9 * expiry` tolerance `observation_indices` already defines, not exact float
    equality -- a monthly TARF-style schedule accumulates real float drift
    (`sum(1/12 for _ in range(24))` is `1.9999999999999991`, not exactly `2.0`)."""
    expiry = 2.0
    obs_list: list[float] = []
    acc = 0.0
    for _ in range(24):
        acc += 1.0 / 12.0
        obs_list.append(acc)
    observations = tuple(obs_list)
    assert observations[-1] != expiry  # sanity: this really is the float-drift case

    Autocallable(
        underlying="SYNTH",
        notional=1000.0,
        observations=observations,
        autocall_trigger=1e9,
        coupon_barrier=1e9,
        coupon_rate=0.01,
        memory=False,
        protection_barrier=100.0,
        initial_level=100.0,
        expiry=expiry,
    )  # must not raise


def test_price_from_bundle_rejects_bundle_horizon_mismatch() -> None:
    """HIGH-1 (code review, fix round 1): `price_from_bundle` had no guard against a bundle
    simulated to a different horizon than the payoff."""
    engine = EngineConfig(scheme="qe", n_steps=20, n_paths=4, expiry=2.0, antithetic=False)
    bundle = simulate(_MSFT, engine, PseudoRandomSource(seed=1, antithetic=False))

    with pytest.raises(ValueError):
        price_from_bundle(_note(memory=False), bundle, r=_MSFT.r)  # note.expiry == 1.0


def test_cashflows_reads_terminal_spot_at_its_own_expiry_not_bundle_last_column() -> None:
    """HIGH-1 (code review, fix round 1): before the fix, the maturity leg read
    `bundle.S[:, -1]` (the bundle's last column) instead of the note's own final observation
    index. This forges a bundle identical up to the note's own expiry index but with garbage
    afterward, and asserts the ledger is unchanged."""
    note = Autocallable(
        underlying="SYNTH",
        notional=1000.0,
        observations=(0.5, 1.0),
        autocall_trigger=1e9,  # unreachable: isolates the terminal-leg indexing
        coupon_barrier=1e9,
        coupon_rate=0.02,
        memory=False,
        protection_barrier=100.0,
        initial_level=100.0,
        expiry=1.0,
    )
    n_steps = 20
    engine = EngineConfig(scheme="qe", n_steps=n_steps, n_paths=2, expiry=2.0, antithetic=False)
    bundle = simulate(
        HestonParams(s0=100.0, r=0.0, q=0.0, v0=0.04, kappa=1.5, theta=0.04, xi=0.6, rho=-0.7),
        engine,
        PseudoRandomSource(seed=1, antithetic=False),
    )
    ledger = note.cashflows(bundle)  # note.observations land on grid indices [5, 10]

    forged_s = bundle.S.copy()
    forged_s[:, 11:] = -999.0  # would corrupt bundle.S[:, -1] if read directly
    forged_bundle = replace(bundle, S=forged_s)
    forged_ledger = note.cashflows(forged_bundle)

    np.testing.assert_array_equal(ledger.amounts, forged_ledger.amounts)
