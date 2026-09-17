"""P2.M1 slice 2's BLOCKING validation gates (exo/CLAUDE.md: "MC vs closed-form agreement
within 3 standard errors is the acceptance test"; ADR-006 Amendment 4 s3).

- G3a: Autocallable with trigger/coupon-barrier below every simulated path -> every path
  redeems at observation 1 -> machine-precision identity, se == 0 exactly.
- G3b: Autocallable with trigger/coupon-barrier unreachably high, protection_barrier ==
  initial_level -> the terminal leg decomposes EXACTLY as a short put struck at s0, so it
  prices against `heston_vanilla_price` within 3 SE.
- G4: Down-and-out call, model degenerated to Black-Scholes, CONTINUOUS_BRIDGE monitoring,
  vs `bs_barrier_price` (Reiner-Rubinstein) within 3 SE. A companion test proves the bridge
  is load-bearing: pricing the SAME term sheet under DISCRETE from the SAME bundle must give
  a materially, not just statistically, different (higher) price.
- G5: In-out parity `KO + KI == vanilla` per path, exact by construction, both monitoring
  modes.

EVERY 3-SE GATE HERE CARRIES A SECOND CONJUNCT BOUNDING THE SE ITSELF (see
`test_validation_gates.py`'s module docstring for why): `tol_abs` is set from the SE actually
measured at this test's n_paths/n_steps, recorded in each gate's own docstring.

Slice 1's gates (`test_validation_gates.py`) are untouched by this slice.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from exo.models.analytic import bs_barrier_price
from exo.models.heston import simulate
from exo.models.heston_cf import heston_vanilla_price
from exo.models.params import EngineConfig, HestonParams
from exo.models.rng import PseudoRandomSource
from exo.products.autocallable import Autocallable
from exo.products.barrier import BarrierOption
from exo.products.base import Monitoring
from exo.products.pricer import discount, price_from_bundle

_AAPL = HestonParams(
    s0=187.50, r=0.0425, q=0.0050, v0=0.0400, kappa=1.50, theta=0.0400, xi=0.60, rho=-0.70
)


def test_g3a_autocallable_certain_call_at_first_observation_is_exact() -> None:
    """G3a. `autocall_trigger` and `coupon_barrier` set at 1.0 -- unreachably far below every
    simulated AAPL path -- so every path calls at the FIRST observation with certainty. The
    discounted cashflow is then the SAME scalar for every path (it depends only on the
    universally-true boolean branch, not on the path's actual level), so the pair-mean
    standard error is exactly 0.0 -- machine precision, not a 3-SE gate."""
    notional = 1000.0
    coupon_rate = 0.05
    t1 = 0.5
    note = Autocallable(
        underlying="AAPL",
        notional=notional,
        observations=(t1, 1.0),
        autocall_trigger=1.0,
        coupon_barrier=1.0,
        coupon_rate=coupon_rate,
        memory=True,
        protection_barrier=1.0,
        initial_level=_AAPL.s0,
        expiry=1.0,
    )
    engine = EngineConfig(scheme="qe", n_steps=50, n_paths=20_000, expiry=1.0, antithetic=True)
    bundle = simulate(_AAPL, engine, PseudoRandomSource(seed=201))

    ledger = note.cashflows(bundle)
    assert np.all(ledger.amounts[:, 0] > 0.0), "every path must call at observation 1"
    assert np.all(ledger.amounts[:, 1] == 0.0), "no cashflow may follow the call"

    result = price_from_bundle(note, bundle, r=_AAPL.r)
    expected = notional * (1.0 + coupon_rate) * math.exp(-_AAPL.r * t1)

    assert result.std_err == 0.0, f"se={result.std_err} must be EXACTLY zero (see docstring)"
    assert result.pv == pytest.approx(expected, rel=0.0, abs=1e-9)


def test_g3b_autocallable_never_called_decomposes_as_short_put_within_3se() -> None:
    """G3b. `autocall_trigger`/`coupon_barrier` set unreachably high (1e7) -- no path ever
    calls or earns a coupon -- and `protection_barrier == initial_level == s0`, which
    collapses the terminal leg to EXACTLY `notional - (notional/s0)*max(s0-S_T, 0)`: a short
    put struck at s0 (see brief s5, "G3b -- read this twice"). Reference PV DISCOUNTS the
    notional leg (`notional*exp(-r*T) - (notional/s0)*heston_vanilla_price(put)`) -- the
    ledger is undiscounted-and-dated by design, so `discount()` applies `exp(-r*T)` to every
    leg including the certain one.

    Measured at n_steps=50, n_paths=20_000, seed=202: pv=904.816, ref=904.865, SE ~= 0.713,
    |mc - ref|/se ~= -0.069. tol_abs=0.8 sits just above the measured SE.
    """
    notional = 1000.0
    s0 = _AAPL.s0
    note = Autocallable(
        underlying="AAPL",
        notional=notional,
        observations=(0.5, 1.0),
        autocall_trigger=1.0e7,
        coupon_barrier=1.0e7,
        coupon_rate=0.05,
        memory=True,
        protection_barrier=s0,
        initial_level=s0,
        expiry=1.0,
    )
    engine = EngineConfig(scheme="qe", n_steps=50, n_paths=20_000, expiry=1.0, antithetic=True)
    bundle = simulate(_AAPL, engine, PseudoRandomSource(seed=202))

    ledger = note.cashflows(bundle)
    assert np.all(ledger.amounts[:, 0] == 0.0), "no path may call or earn a coupon"

    result = price_from_bundle(note, bundle, r=_AAPL.r)
    reference = notional * math.exp(-_AAPL.r * 1.0) - (notional / s0) * heston_vanilla_price(
        _AAPL, strike=s0, expiry=1.0, is_call=False
    )

    tol_abs = 0.8
    assert 0.0 < result.std_err < tol_abs, (
        f"se={result.std_err} is not tight enough to be a meaningful gate (tol_abs={tol_abs})"
    )
    assert abs(result.pv - reference) < 3.0 * result.std_err, (
        f"pv={result.pv}, ref={reference}, off by {(result.pv - reference) / result.std_err:.2f} SE"
    )


_SIGMA_DEGENERATE = 0.2
_DEGENERATE = HestonParams(
    s0=100.0,
    r=0.02,
    q=0.01,
    v0=_SIGMA_DEGENERATE**2,
    kappa=1.5,
    theta=_SIGMA_DEGENERATE**2,
    xi=1e-4,
    rho=0.0,
)
_G4_STRIKE = 100.0
_G4_BARRIER = 90.0
_G4_EXPIRY = 1.0
_G4_N_STEPS = 50


def _g4_barrier_option(
    monitoring: Monitoring, observations: tuple[float, ...] | None
) -> BarrierOption:
    return BarrierOption(
        underlying="SYN",
        option_type="call",
        strike=_G4_STRIKE,
        expiry=_G4_EXPIRY,
        barrier=_G4_BARRIER,
        direction="down",
        knock="out",
        monitoring=monitoring,
        observations=observations,
    )


@pytest.mark.parametrize("seed", [42, 7, 123, 2024])
def test_g4_down_and_out_call_matches_bs_barrier_within_3se(seed: int) -> None:
    """G4. Down-and-out call, model degenerated to Black-Scholes (same degeneration as slice
    1's G2: xi -> ~0, v0 = theta = sigma**2, rho = 0), CONTINUOUS_BRIDGE monitoring, vs
    `bs_barrier_price` (Reiner-Rubinstein). The Brownian-bridge survival weight is exact (not
    approximate) in this degenerate limit, so this gate is bias-free at ANY path count (see
    brief s2, "Why the monitoring decision is the crux").

    MEDIUM-2 (code review, fix round 1): a single committed seed consumes roughly half the
    3-SE budget (see below) and is one dependency bump away from a coin-flip draw --
    `rng.py`'s own ponytail records that NEP 19 does not guarantee `Generator` reproducibility
    across numpy versions. Parametrized over 4 seeds (rather than pooling via
    `PriceResult.combine`) so this gate also exercises the RNG seam per seed.

    Measured at n_steps=50, n_paths=20_000 (this repo, fix round 1):
        seed=42:   pv=6.689759  se=0.076642  z=-1.539
        seed=7:    pv=6.842298  se=0.078742  z=+0.439
        seed=123:  pv=6.773450  se=0.077402  z=-0.443
        seed=2024: pv=6.819603  se=0.077761  z=+0.153
    (bs_ref=6.807708 throughout). tol_abs=0.09 sits just above every measured SE.

    Independently measured by the code reviewer across 12 seeds at this same path count: mean
    pv bias = +0.0226, se(mean) = 0.0229 -> bias/se = +0.99 -- statistically indistinguishable
    from zero. Critically, z does NOT grow with path count (seed 42: 20k z=-1.54, 80k z=-1.01,
    320k z=+0.26) or step count (seed 42, 25/50/100/252 steps -> z=+1.14/-1.54/-1.23/-1.14),
    which is the actual evidence for "the bridge is exact at any path count" -- a claim this
    gate asserted before fix round 1 without a number behind it.
    """
    engine = EngineConfig(
        scheme="qe", n_steps=_G4_N_STEPS, n_paths=20_000, expiry=_G4_EXPIRY, antithetic=True
    )
    bundle = simulate(_DEGENERATE, engine, PseudoRandomSource(seed=seed))
    barrier_opt = _g4_barrier_option(Monitoring.CONTINUOUS_BRIDGE, None)

    result = price_from_bundle(barrier_opt, bundle, r=_DEGENERATE.r)
    reference = bs_barrier_price(
        s0=_DEGENERATE.s0,
        strike=_G4_STRIKE,
        r=_DEGENERATE.r,
        q=_DEGENERATE.q,
        sigma=_SIGMA_DEGENERATE,
        expiry=_G4_EXPIRY,
        barrier=_G4_BARRIER,
        direction="down",
        knock="out",
        option_type="call",
    )

    tol_abs = 0.09
    assert 0.0 < result.std_err < tol_abs, (
        f"seed={seed}: se={result.std_err} is not tight enough to be a meaningful gate "
        f"(tol_abs={tol_abs})"
    )
    assert abs(result.pv - reference) < 3.0 * result.std_err, (
        f"seed={seed}: pv={result.pv}, bs_ref={reference}, off by "
        f"{(result.pv - reference) / result.std_err:.2f} SE"
    )


def test_g4_companion_bridge_is_load_bearing_paired_difference() -> None:
    """G4 companion. Prices the SAME down-and-out term sheet twice -- CONTINUOUS_BRIDGE and
    DISCRETE -- from ONE shared `PathBundle` (via `price_from_bundle`'s underlying
    `discount()`, called directly here so the two payoff vectors stay perfectly correlated),
    then collapses the per-path difference `d = disc_discrete - disc_continuous` to
    antithetic pair means (same construction as `test_validation_gates.py:134-137`'s
    QE-vs-Euler paired difference). This is a PAIRED-difference assertion specifically so it
    cannot be flaky at 20k paths (brief s5): an unpaired 3-SE comparison of two independently-
    noisy prices would occasionally fail to distinguish them even when the bridge is doing
    real work.

    Discrete monitoring misses intra-step crossings, so it must knock out LESS often and
    price HIGHER: `d_mean` must be positive and many SE from zero.

    Measured at n_steps=50, n_paths=20_000, seed=42: d_mean ~= 0.4263, se_diff ~= 0.01789,
    d_mean/se_diff ~= 23.8, and (from the gate above, computed against the SAME bs_ref) the
    discrete price's own |mc_discrete - bs_ref|/se ~= 4.01 -- i.e. DISCRETE monitoring alone
    would FAIL a 3-SE gate against the continuous-monitoring closed form (which is exactly the
    point: they are different contracts), recorded here for context, not asserted.
    """
    engine = EngineConfig(
        scheme="qe", n_steps=_G4_N_STEPS, n_paths=20_000, expiry=_G4_EXPIRY, antithetic=True
    )
    bundle = simulate(_DEGENERATE, engine, PseudoRandomSource(seed=42))

    observations = tuple(float(i) / _G4_N_STEPS for i in range(1, _G4_N_STEPS + 1))
    continuous = _g4_barrier_option(Monitoring.CONTINUOUS_BRIDGE, None)
    discrete = _g4_barrier_option(Monitoring.DISCRETE, observations)

    disc_continuous = discount(continuous.cashflows(bundle), r=_DEGENERATE.r)
    disc_discrete = discount(discrete.cashflows(bundle), r=_DEGENERATE.r)

    d = disc_discrete - disc_continuous
    n_pairs = bundle.n_pairs
    assert n_pairs is not None
    pair_means = 0.5 * (d[:n_pairs] + d[n_pairs:])
    d_mean = float(pair_means.mean())
    se_diff = float(pair_means.std(ddof=1) / math.sqrt(n_pairs))

    tol_abs = 0.025
    assert 0.0 < se_diff < tol_abs, (
        f"se_diff={se_diff} is not tight enough to be a meaningful gate (tol_abs={tol_abs})"
    )
    assert d_mean > 0.0, f"discrete monitoring must price ABOVE continuous, got d_mean={d_mean}"
    assert abs(d_mean) > 3.0 * se_diff, (
        f"d_mean={d_mean}, se_diff={se_diff}, off by {d_mean / se_diff:.2f} paired SE -- "
        "if this fails, the bridge is not doing real work"
    )


@pytest.mark.parametrize(
    ("monitoring", "observations"),
    [
        (Monitoring.CONTINUOUS_BRIDGE, None),
        (Monitoring.DISCRETE, tuple(float(i) / 50 for i in range(1, 51))),
    ],
)
def test_g5_in_out_parity_exact_per_path(
    monitoring: Monitoring, observations: tuple[float, ...] | None
) -> None:
    """G5. `KO + KI == vanilla` per path, EXACT by construction: the ledger weight is `w =
    survival` for knock-out and `w = 1 - survival` for knock-in, computed from the identical
    `barrier_survival` call in both term sheets, so `w_ko*intrinsic + w_ki*intrinsic ==
    intrinsic` regardless of what `survival` actually is. Machine precision, not a 3-SE gate;
    holds under BOTH monitoring modes."""
    engine = EngineConfig(scheme="qe", n_steps=50, n_paths=2_000, expiry=1.0, antithetic=True)
    bundle = simulate(_AAPL, engine, PseudoRandomSource(seed=99))

    knock_out = BarrierOption(
        underlying="AAPL",
        option_type="call",
        strike=_AAPL.s0,
        expiry=1.0,
        barrier=_AAPL.s0 * 0.85,
        direction="down",
        knock="out",
        monitoring=monitoring,
        observations=observations,
    )
    knock_in = BarrierOption(
        underlying="AAPL",
        option_type="call",
        strike=_AAPL.s0,
        expiry=1.0,
        barrier=_AAPL.s0 * 0.85,
        direction="down",
        knock="in",
        monitoring=monitoring,
        observations=observations,
    )

    ko_amounts = knock_out.cashflows(bundle).amounts[:, 0]
    ki_amounts = knock_in.cashflows(bundle).amounts[:, 0]
    vanilla_payoff = np.maximum(bundle.S[:, -1] - _AAPL.s0, 0.0)

    np.testing.assert_allclose(ko_amounts + ki_amounts, vanilla_payoff, rtol=1e-12, atol=1e-9)


def test_price_via_package_surface_happy_path_and_expiry_mismatch() -> None:
    """MEDIUM-3 (code review, fix round 1): no test imported `exo.products.price` or the
    `exo.products` package surface at all before this fix -- every other test in this slice
    imports submodules directly and uses `price_from_bundle`. So `price()`'s own
    expiry-mismatch guard, and the `products/__init__.py` re-export list, were both untested.
    Imports via `from exo.products import ...` specifically so the re-export list is
    exercised, not just the submodule it forwards to.
    """
    from exo.products import BarrierOption as PackageBarrierOption
    from exo.products import Monitoring as PackageMonitoring
    from exo.products import price as package_price

    opt = PackageBarrierOption(
        underlying="AAPL",
        option_type="call",
        strike=_AAPL.s0,
        expiry=1.0,
        barrier=_AAPL.s0 * 0.85,
        direction="down",
        knock="out",
        monitoring=PackageMonitoring.CONTINUOUS_BRIDGE,
        observations=None,
    )
    engine = EngineConfig(scheme="qe", n_steps=50, n_paths=2_000, expiry=1.0, antithetic=True)

    happy_result = package_price(opt, _AAPL, engine, PseudoRandomSource(seed=99))
    assert happy_result.std_err > 0.0
    assert happy_result.n_paths == 2_000

    mismatched_engine = EngineConfig(
        scheme="qe", n_steps=50, n_paths=2_000, expiry=2.0, antithetic=True
    )
    with pytest.raises(ValueError):
        package_price(opt, _AAPL, mismatched_engine, PseudoRandomSource(seed=99))
