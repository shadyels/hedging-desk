"""P2.M2 slice 1's BLOCKING validation gates for the certificate payoffs and the European
warrant (exo/CLAUDE.md: "MC vs closed-form agreement within 3 standard errors is the
acceptance test").

- G-RC1: reverse convertible with strike 1.0 -- no path converts -- so the PV is a sum of
  certain cashflows on FOUR distinct dates: an exact identity (se ~ 1e-15), sharp against any
  per-column discount-factor or t-to-column off-by-one (closes ADR-006 A6 s9's multi-column
  discount gap, PONYTAIL-DEBT.md item (4)).
- G-RC2: reverse convertible struck at s0 vs coupons + zero-coupon bond - (N/K) * Heston put.
- G-BRC / G-BRC-par: barrier reverse convertible vs Reiner-Rubinstein (BS-degenerate), and the
  per-path identity BRC - RC == (N/K) * down-and-out put, both monitoring modes.
- G-BON / G-BON-par: bonus certificate vs ratio * (S0 e^{-qT} + down-and-out put), and the
  per-path identity bonus - ratio * S_T == ratio * down-and-out put, both monitoring modes.
- G-W1: European warrant, call and put, three strikes, on `[models.SPX]` (Feller ratio
  0.231) vs `ratio * heston_vanilla_price`; closes the SPX Feller-violating carried risk.

EVERY 3-SE GATE CARRIES A SECOND CONJUNCT BOUNDING THE SE ITSELF (`0 < se < tol_abs`, see
`test_validation_gates.py`'s module docstring): `tol_abs` is set from the SE actually measured
at each test's n_paths/n_steps, recorded in each gate's own docstring.
"""

from __future__ import annotations

import math
import tomllib
from pathlib import Path
from typing import Literal

import numpy as np
import pytest

from exo.models.analytic import bs_barrier_price
from exo.models.heston import simulate
from exo.models.heston_cf import heston_call_price_and_error, heston_vanilla_price
from exo.models.params import EngineConfig, HestonParams, load_models_section
from exo.models.rng import PseudoRandomSource
from exo.products.barrier import BarrierOption
from exo.products.base import Monitoring
from exo.products.bonus import BonusCertificate
from exo.products.pricer import price_from_bundle
from exo.products.reverse_convertible import ReverseConvertible
from exo.products.warrant import Warrant

_MODELS = load_models_section(
    tomllib.loads((Path(__file__).parents[1] / "exo.toml").read_text())["models"]
)[1]
_AAPL = _MODELS["AAPL"]
_SPX = _MODELS["SPX"]

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

_COUPON_TIMES = (0.25, 0.5, 0.75, 1.0)
_BRC_COUPON_TIMES = (0.5, 1.0)
_N_STEPS = 50
_ALL_STEPS = tuple(float(i) / _N_STEPS for i in range(1, _N_STEPS + 1))


def _engine(n_steps: int, n_paths: int = 20_000) -> EngineConfig:
    return EngineConfig(scheme="qe", n_steps=n_steps, n_paths=n_paths, expiry=1.0, antithetic=True)


def test_g_rc1_no_conversion_four_coupon_dates_is_exact() -> None:
    """G-RC1. `[models.AAPL]`, notional 1000, strike 1.0 -- unreachably far below every
    simulated path, so `min(1, S_T/K) == 1` and NO path converts -- coupons of 20 at
    (0.25, 0.5, 0.75, 1.0). Every path pays the same four certain cashflows, so the PV is
    `20 * sum(exp(-r t_i)) + 1000 * exp(-r)` with se equal to float noise. Any wrong per-column
    discount factor or t-to-column off-by-one moves this by >> 1e-9. n_steps=52 (not 50) so
    all four coupon dates land on the grid (0.25 = 13/52).

    Measured at n_steps=52, n_paths=20_000, seed=301: pv=1036.298936617906,
    se=2.27e-15, pv - ref = 2.3e-13.
    """
    note = ReverseConvertible(
        underlying="AAPL",
        notional=1000.0,
        strike=1.0,
        coupon_rate=0.02,
        coupon_times=_COUPON_TIMES,
        expiry=1.0,
    )
    bundle = simulate(_AAPL, _engine(52), PseudoRandomSource(seed=301))
    result = price_from_bundle(note, bundle, r=_AAPL.r)

    reference = 20.0 * sum(math.exp(-_AAPL.r * t) for t in _COUPON_TIMES) + 1000.0 * math.exp(
        -_AAPL.r
    )
    assert result.std_err <= 1e-12, f"se={result.std_err} must be float noise"
    assert abs(result.pv - reference) <= 1e-9, f"pv={result.pv}, ref={reference}"


def test_g_rc2_reverse_convertible_decomposes_as_short_put_within_3se() -> None:
    """G-RC2. Strike == s0: the terminal leg is `N - (N/s0) * (s0 - S_T)^+`, so PV = coupons
    + `N * exp(-rT) - (N/s0) * heston_vanilla_price(put)`. The ledger is undiscounted, so
    the certain notional leg IS discounted in the reference. Same random leg as G3b, hence
    G3b's tol_abs.

    Measured at n_steps=52, n_paths=20_000, seed=302: pv=982.967567, se=0.713179,
    z=+0.272. tol_abs=0.8 sits just above the measured SE.
    """
    note = ReverseConvertible(
        underlying="AAPL",
        notional=1000.0,
        strike=_AAPL.s0,
        coupon_rate=0.02,
        coupon_times=_COUPON_TIMES,
        expiry=1.0,
    )
    bundle = simulate(_AAPL, _engine(52), PseudoRandomSource(seed=302))
    result = price_from_bundle(note, bundle, r=_AAPL.r)

    reference = (
        20.0 * sum(math.exp(-_AAPL.r * t) for t in _COUPON_TIMES)
        + 1000.0 * math.exp(-_AAPL.r)
        - (1000.0 / _AAPL.s0)
        * heston_vanilla_price(_AAPL, strike=_AAPL.s0, expiry=1.0, is_call=False)
    )
    tol_abs = 0.8
    assert 0.0 < result.std_err < tol_abs, (
        f"se={result.std_err} is not tight enough to be a meaningful gate (tol_abs={tol_abs})"
    )
    assert abs(result.pv - reference) < 3.0 * result.std_err, (
        f"pv={result.pv}, ref={reference}, off by {(result.pv - reference) / result.std_err:.2f} SE"
    )


def _brc(monitoring: Monitoring, observations: tuple[float, ...] | None) -> ReverseConvertible:
    return ReverseConvertible(
        underlying="SYN",
        notional=1000.0,
        strike=100.0,
        coupon_rate=0.03,
        coupon_times=_BRC_COUPON_TIMES,
        expiry=1.0,
        barrier=80.0,
        monitoring=monitoring,
        observations=observations,
    )


@pytest.mark.parametrize("seed", [42, 7, 123, 2024])
def test_g_brc_barrier_reverse_convertible_matches_reiner_rubinstein_within_3se(
    seed: int,
) -> None:
    """G-BRC. BS-degenerate model (G4's `_DEGENERATE`), N=1000, K=100, B=80, coupons of 30 at
    (0.5, 1.0), CONTINUOUS_BRIDGE. BRC PV = coupons + `N e^{-rT} - (N/K) * down-and-in put(K,
    B)` (the put only knocks in if the barrier is touched). Sharp against a knock-in weight
    sign error and the down-put K>H Reiner-Rubinstein cell. The bridge is exact in the
    degenerate limit, so this gate is bias-free at any path count.

    Measured at n_steps=50, n_paths=20_000 (ref from `bs_barrier_price`):
        seed=42:   pv=985.078769  se=0.596117  z=+0.855
        seed=7:    pv=984.355622  se=0.602640  z=-0.354
        seed=123:  pv=984.523233  se=0.596887  z=-0.077
        seed=2024: pv=984.777988  se=0.598749  z=+0.349
    tol_abs=0.7 sits just above every measured SE.
    """
    note = _brc(Monitoring.CONTINUOUS_BRIDGE, None)
    bundle = simulate(_DEGENERATE, _engine(_N_STEPS), PseudoRandomSource(seed=seed))
    result = price_from_bundle(note, bundle, r=_DEGENERATE.r)

    knock_in_put = bs_barrier_price(
        s0=_DEGENERATE.s0,
        strike=100.0,
        r=_DEGENERATE.r,
        q=_DEGENERATE.q,
        sigma=_SIGMA_DEGENERATE,
        expiry=1.0,
        barrier=80.0,
        direction="down",
        knock="in",
        option_type="put",
    )
    reference = (
        30.0 * sum(math.exp(-_DEGENERATE.r * t) for t in _BRC_COUPON_TIMES)
        + 1000.0 * math.exp(-_DEGENERATE.r)
        - 10.0 * knock_in_put
    )
    tol_abs = 0.7
    assert 0.0 < result.std_err < tol_abs, (
        f"seed={seed}: se={result.std_err} is not tight enough to be a meaningful gate "
        f"(tol_abs={tol_abs})"
    )
    assert abs(result.pv - reference) < 3.0 * result.std_err, (
        f"seed={seed}: pv={result.pv}, ref={reference}, off by "
        f"{(result.pv - reference) / result.std_err:.2f} SE"
    )


@pytest.mark.parametrize(
    ("monitoring", "observations"),
    [(Monitoring.CONTINUOUS_BRIDGE, None), (Monitoring.DISCRETE, _ALL_STEPS)],
)
def test_g_brc_par_brc_minus_rc_equals_down_and_out_put_per_path(
    monitoring: Monitoring, observations: tuple[float, ...] | None
) -> None:
    """G-BRC-par. `BRC_T - RC_T == (N/K) * (K - S_T)^+ * surv` per path, i.e. `(N/K)` times
    the down-and-out put ledger of the SAME bundle: both are built from one `barrier_survival`
    call, so this is exact to machine precision under BOTH monitoring modes. Sharp against a
    `surv` vs `1 - surv` swap and DISCRETE schedule bugs."""
    bundle = simulate(_DEGENERATE, _engine(_N_STEPS), PseudoRandomSource(seed=42))
    brc = _brc(monitoring, observations).cashflows(bundle).amounts
    rc = (
        ReverseConvertible(
            underlying="SYN",
            notional=1000.0,
            strike=100.0,
            coupon_rate=0.03,
            coupon_times=_BRC_COUPON_TIMES,
            expiry=1.0,
        )
        .cashflows(bundle)
        .amounts
    )
    dop = (
        BarrierOption(
            underlying="SYN",
            option_type="put",
            strike=100.0,
            expiry=1.0,
            barrier=80.0,
            direction="down",
            knock="out",
            monitoring=monitoring,
            observations=observations,
        )
        .cashflows(bundle)
        .amounts
    )

    np.testing.assert_allclose(
        brc[:, -1] - rc[:, -1], (1000.0 / 100.0) * dop[:, 0], rtol=1e-12, atol=1e-9
    )
    np.testing.assert_array_equal(brc[:, 0], rc[:, 0])  # coupon column untouched by the barrier


def _bonus(monitoring: Monitoring, observations: tuple[float, ...] | None) -> BonusCertificate:
    return BonusCertificate(
        underlying="SYN",
        ratio=0.5,
        bonus_level=115.0,
        barrier=75.0,
        expiry=1.0,
        monitoring=monitoring,
        observations=observations,
    )


@pytest.mark.parametrize("seed", [42, 7, 123, 2024])
def test_g_bon_bonus_certificate_matches_reiner_rubinstein_within_3se(seed: int) -> None:
    """G-BON. BS-degenerate model, ratio 0.5, bonus level 115, barrier 75, CONTINUOUS_BRIDGE.
    PV = `ratio * (S0 e^{-qT} + down-and-out put(K=115, H=75))`. Sharp against paying the bonus
    on knocked paths and against ratio scaling.

    Measured at n_steps=50, n_paths=20_000:
        seed=42:   pv=54.825685  se=0.027444  z=-1.183
        seed=7:    pv=54.841725  se=0.027398  z=-0.600
        seed=123:  pv=54.863211  se=0.027268  z=+0.185
        seed=2024: pv=54.885588  se=0.027250  z=+1.006
    tol_abs=0.032 sits just above every measured SE.
    """
    bundle = simulate(_DEGENERATE, _engine(_N_STEPS), PseudoRandomSource(seed=seed))
    result = price_from_bundle(_bonus(Monitoring.CONTINUOUS_BRIDGE, None), bundle, r=_DEGENERATE.r)

    dop = bs_barrier_price(
        s0=_DEGENERATE.s0,
        strike=115.0,
        r=_DEGENERATE.r,
        q=_DEGENERATE.q,
        sigma=_SIGMA_DEGENERATE,
        expiry=1.0,
        barrier=75.0,
        direction="down",
        knock="out",
        option_type="put",
    )
    reference = 0.5 * (_DEGENERATE.s0 * math.exp(-_DEGENERATE.q) + dop)
    tol_abs = 0.032
    assert 0.0 < result.std_err < tol_abs, (
        f"seed={seed}: se={result.std_err} is not tight enough to be a meaningful gate "
        f"(tol_abs={tol_abs})"
    )
    assert abs(result.pv - reference) < 3.0 * result.std_err, (
        f"seed={seed}: pv={result.pv}, ref={reference}, off by "
        f"{(result.pv - reference) / result.std_err:.2f} SE"
    )


@pytest.mark.parametrize(
    ("monitoring", "observations"),
    [(Monitoring.CONTINUOUS_BRIDGE, None), (Monitoring.DISCRETE, _ALL_STEPS)],
)
def test_g_bon_par_bonus_minus_underlying_equals_down_and_out_put_per_path(
    monitoring: Monitoring, observations: tuple[float, ...] | None
) -> None:
    """G-BON-par. `bonus - ratio * S_T == ratio * DOP(L, B)` per path, machine precision,
    both monitoring modes (one shared `barrier_survival`)."""
    bundle = simulate(_DEGENERATE, _engine(_N_STEPS), PseudoRandomSource(seed=42))
    bonus = _bonus(monitoring, observations).cashflows(bundle).amounts[:, 0]
    dop = (
        BarrierOption(
            underlying="SYN",
            option_type="put",
            strike=115.0,
            expiry=1.0,
            barrier=75.0,
            direction="down",
            knock="out",
            monitoring=monitoring,
            observations=observations,
        )
        .cashflows(bundle)
        .amounts[:, 0]
    )

    np.testing.assert_allclose(bonus - 0.5 * bundle.S[:, -1], 0.5 * dop, rtol=1e-12, atol=1e-9)


@pytest.mark.parametrize("option_type", ["call", "put"])
@pytest.mark.parametrize("moneyness", [0.9, 1.0, 1.1])
def test_g_w1_european_warrant_matches_heston_vanilla_within_3se(
    option_type: Literal["call", "put"], moneyness: float
) -> None:
    """G-W1. `[models.SPX]` (Feller ratio 0.231, the second Feller-violating set), ratio 0.01,
    K in {0.9, 1.0, 1.1} * s0, call and put, QE / 50 steps / 20k paths, vs `ratio *
    heston_vanilla_price`. The ratio is 1e-2 on a 5500-point index: a warrant that applied the
    ratio as a parity-style 1e4 scaling, or dropped it, misses by orders of magnitude. Also
    asserts the CF quadrature's own error estimate (`abserr < 1e-10`) so the reference is
    trustworthy. Closes the SPX Feller-violating carried risk: QE holds at 0.231 on all six
    cells.

    Measured at the gate's seed=401 (z, SE): call 0.9 +0.28, 0.0308; call 1.0 +0.19, 0.0240;
    call 1.1 +0.00, 0.0174; put 0.9 +0.82, 0.0308; put 1.0 +0.54, 0.0393; put 1.1 +0.37,
    0.0449. Separately, over a 20-seed sweep
    (seeds 500..519; one shared bundle per seed, all six cells) -- per cell: mean z / max|z|
    / SE (mean over seeds):
        call 0.9: +0.05 / 1.82 / 0.0308    put 0.9: +0.02 / 1.44 / 0.0304
        call 1.0: -0.02 / 1.67 / 0.0240    put 1.0: -0.03 / 1.92 / 0.0389
        call 1.1: -0.24 / 2.36 / 0.0172    put 1.1: -0.11 / 2.05 / 0.0447
    Pooled over strikes: call mean z -0.07, max|z| 2.36; put mean z -0.04, max|z| 2.05.
    Largest SE per type: call 0.0318, put 0.0453 -> tol_abs call 0.035, put 0.05. CF abserr
    <= 9e-13 at all three strikes.
    """
    strike = moneyness * _SPX.s0
    _, abserr = heston_call_price_and_error(_SPX, strike, 1.0)
    assert abserr < 1e-10, f"CF reference not converged: abserr={abserr}"

    warrant = Warrant(
        underlying="SPX",
        option_type=option_type,
        strike=strike,
        expiry=1.0,
        ratio=0.01,
        exercise="european",
    )
    bundle = simulate(_SPX, _engine(_N_STEPS), PseudoRandomSource(seed=401))
    result = price_from_bundle(warrant, bundle, r=_SPX.r)
    reference = 0.01 * heston_vanilla_price(
        _SPX, strike=strike, expiry=1.0, is_call=option_type == "call"
    )

    tol_abs = 0.035 if option_type == "call" else 0.05
    assert 0.0 < result.std_err < tol_abs, (
        f"{option_type} {moneyness}: se={result.std_err} is not tight enough to be a "
        f"meaningful gate (tol_abs={tol_abs})"
    )
    assert abs(result.pv - reference) < 3.0 * result.std_err, (
        f"{option_type} {moneyness}: pv={result.pv}, ref={reference}, off by "
        f"{(result.pv - reference) / result.std_err:.2f} SE"
    )
