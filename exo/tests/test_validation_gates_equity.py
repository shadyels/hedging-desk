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
- G-MF1 / G-MF2 (P2.M2 slice 2): mini future, long and short. G-MF1 prices the knock-out +
  residual decomposition against `bs_barrier_price` + `bs_barrier_rebate_at_hit`; G-MF2 pins the
  financing accrual, reset schedule and reset-jump stop price via the martingale identity
  `PV = ratio * (S0 - F0)`. COVERAGE CAVEAT: both are BLIND to per-column discount
  differentiation -- a "discount every column at T" mutant moves the 20-seed mean z only
  -0.12 -> -0.68 (SE of the mean ~0.22; measured on G-MF1 long q=0) because the mutant only
  re-dates the small stopped-out residual, an effect of order `ratio * |B - F| * r * E[T - tau] *
  P_hit` ~ 0.5 SE -- G-RC1 is the gate that pins discounting.
- G-W1: European warrant, call and put, three strikes, on `[models.SPX]` (Feller ratio
  0.231) vs `ratio * heston_vanilla_price`; closes the SPX Feller-violating carried risk.
- G-W2 / G-W3 / G-W4 (P2.M2 slice 3): American warrants by Longstaff-Schwartz. G-W2 is the exact
  zero-dividend-call identity (pins the Jensen floor and terminal plumbing, NOT the regression);
  G-W3 is the ONE-SIDED Bermudan-binomial gate plus a declared low-bias budget (a two-sided 3-SE
  gate is forbidden, ADR-006 A7 s3); G-W4 shows the (S, v) basis beats an S-only basis under
  Heston.

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
from numpy.typing import NDArray

from exo.models.analytic import bs_barrier_price, bs_barrier_rebate_at_hit
from exo.models.binomial import crr_price
from exo.models.estimator import mc_estimate
from exo.models.heston import simulate
from exo.models.heston_cf import heston_call_price_and_error, heston_vanilla_price
from exo.models.lsm import apply_exercise_policy, fit_exercise_policy
from exo.models.params import EngineConfig, HestonParams, load_models_section
from exo.models.rng import PseudoRandomSource
from exo.products.barrier import BarrierOption
from exo.products.base import Monitoring
from exo.products.bonus import BonusCertificate
from exo.products.mini_future import MiniFuture
from exo.products.pricer import discount, price_american, price_from_bundle
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
    brc = _brc(monitoring, observations).cashflows(bundle, r=_DEGENERATE.r).amounts
    rc = (
        ReverseConvertible(
            underlying="SYN",
            notional=1000.0,
            strike=100.0,
            coupon_rate=0.03,
            coupon_times=_BRC_COUPON_TIMES,
            expiry=1.0,
        )
        .cashflows(bundle, r=_DEGENERATE.r)
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
        .cashflows(bundle, r=_DEGENERATE.r)
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
    bonus = _bonus(monitoring, observations).cashflows(bundle, r=_DEGENERATE.r).amounts[:, 0]
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
        .cashflows(bundle, r=_DEGENERATE.r)
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


_MF_R = 0.0425
_MF_RATIO = 0.1


def _mf_params(q: float) -> HestonParams:
    """BS-degenerate model (G4's degeneration) at r=0.0425 and the given dividend yield."""
    return HestonParams(
        s0=100.0,
        r=_MF_R,
        q=q,
        v0=_SIGMA_DEGENERATE**2,
        kappa=1.5,
        theta=_SIGMA_DEGENERATE**2,
        xi=1e-4,
        rho=0.0,
    )


def _mf_term_sheet(
    side: Literal["long", "short"], *, spread: float, reset_times: tuple[float, ...]
) -> MiniFuture:
    """Long: F0=80, B0=85 (stop on a fall); short: F0=120, B0=115 (stop on a rise)."""
    long = side == "long"
    return MiniFuture(
        underlying="SYN",
        side=side,
        ratio=_MF_RATIO,
        financing_level=80.0 if long else 120.0,
        financing_spread=spread,
        stop_loss_level=85.0 if long else 115.0,
        stop_loss_buffer=0.05,
        reset_times=reset_times,
        horizon=1.0,
    )


@pytest.mark.parametrize("seed", [42, 7, 123, 2024])
@pytest.mark.parametrize("q", [0.0, 0.01])
@pytest.mark.parametrize("side", ["long", "short"])
def test_g_mf1_mini_future_without_resets_matches_knock_out_plus_rebate_within_3se(
    side: Literal["long", "short"], q: float, seed: int
) -> None:
    """G-MF1. Financing held CONSTANT (spread = -r long / +r short, so rate_F = 0), no resets,
    horizon 1, ratio 0.1. A long mini future (F0=80, stop B0=85, S0=100) is a down-and-out call
    struck at F0 (< B0, so survivors pay exactly `S_T - F0`) plus a residual `B0 - F0` paid AT
    the hit: `ratio * [DOC(80, 85) + 5 * rebate_at_hit(down, 85)]`. Short (F0=120, B0=115):
    `ratio * [UOP(120, 115) + 5 * rebate_at_hit(up, 115)]`. Sharp against the knock-out leg
    and the residual leg, both sides; exercises the down-call K<H and up-put K>H
    Reiner-Rubinstein cells and the rebate formula, which are pinned independently by
    `test_analytic_barrier.py`'s optional-stopping identities.

    Measured at n_steps=50, n_paths=20_000 (se in ratio-0.1 units; ratio 1 gives ~0.074 long /
    ~0.062 short):
        long  q=0.00: seed 42/7/123/2024 z=-1.53/-0.08/-0.75/-0.28, se=0.00737-0.00757
        long  q=0.01: z=-1.58/-0.06/-0.67/-0.33, se=0.00742-0.00762
        short q=0.00: z=-1.73/-0.17/-0.80/+0.13, se=0.00618-0.00627
        short q=0.01: z=-1.62/-0.41/-0.78/-0.00, se=0.00607-0.00617
    A separate 20-seed sweep (seeds 1000-1019): mean z -0.12 / -0.20 / -0.12 (long q=0, short
    q=0, long q=0.01), max |z| 2.17, 1.70, 2.13 -- unbiased within noise. tol_abs sits just above
    every measured SE (long 0.009, short 0.0075).

    COVERAGE CAVEAT: this gate is BLIND to per-column discount differentiation. A mutant that
    discounts every column at T instead of its own date moves the long q=0 20-seed mean z only
    from -0.12 to -0.68 (SE of the mean ~0.22; max |z| 2.13, so no single seed fails). The
    reason is magnitude, not cancellation (spread = -+r here, so rate_F = 0 and F does not
    accrue): the mutant only re-dates the stopped-out residual, whose effect is of order
    `ratio * |B - F| * r * E[T - tau] * P_hit` = 5 * 0.1 * r * E[T - tau] * ~0.4 ~ 0.004
    ~ 0.57 SE, matching the measured -0.56 shift. G-RC1 is the gate that pins discounting.
    """
    params = _mf_params(q)
    spread = -_MF_R if side == "long" else _MF_R
    note = _mf_term_sheet(side, spread=spread, reset_times=())
    bundle = simulate(params, _engine(_N_STEPS), PseudoRandomSource(seed=seed))
    result = price_from_bundle(note, bundle, r=_MF_R)

    barrier, f0 = note.stop_loss_level, note.financing_level
    rebate = bs_barrier_rebate_at_hit(
        s0=params.s0,
        r=_MF_R,
        q=q,
        sigma=_SIGMA_DEGENERATE,
        expiry=1.0,
        barrier=barrier,
        direction="down" if side == "long" else "up",
    )
    knocked_out = bs_barrier_price(
        s0=params.s0,
        strike=f0,
        r=_MF_R,
        q=q,
        sigma=_SIGMA_DEGENERATE,
        expiry=1.0,
        barrier=barrier,
        direction="down" if side == "long" else "up",
        knock="out",
        option_type="call" if side == "long" else "put",
    )
    reference = _MF_RATIO * (knocked_out + abs(barrier - f0) * rebate)

    tol_abs = 0.009 if side == "long" else 0.0075
    assert 0.0 < result.std_err < tol_abs, (
        f"{side} q={q} seed={seed}: se={result.std_err} is not tight enough to be a meaningful "
        f"gate (tol_abs={tol_abs})"
    )
    assert abs(result.pv - reference) < 3.0 * result.std_err, (
        f"{side} q={q} seed={seed}: pv={result.pv}, ref={reference}, off by "
        f"{(result.pv - reference) / result.std_err:.2f} SE"
    )


@pytest.mark.parametrize("seed", [42, 7, 123, 2024])
@pytest.mark.parametrize("side", ["long", "short"])
def test_g_mf2_mini_future_with_resets_is_worth_spot_minus_financing_within_3se(
    side: Literal["long", "short"], seed: int
) -> None:
    """G-MF2. q=0 and financing spread 0 (rate_F = r), buffer 0.05, stop-loss reset at
    t = 0.08 k, k=1..12 (on the n_steps=50 grid), else G-MF1's fixture. Then the discounted
    financing level `e^{-rt} F_t = F0` is constant and the discounted spot is a martingale, so
    the PV is `ratio * (S0 - F0)` (long) / `ratio * (F0 - S0)` (short) for ANY stopping rule --
    including resets: `E[e^{-r tau} (S_tau - F_tau)] = S0 - F0`. Sharp against a wrong financing
    accrual, reset schedule or reset-jump stop price (each leaves a stopping-rule-dependent
    residual).

    Measured at n_steps=50, n_paths=20_000 (ref = +2.0 long, +2.0 short):
        long  seed 42/7/123/2024: z=-1.88/-0.22/-0.84/-0.63, se=0.00754-0.00773
        short seed 42/7/123/2024: z=-1.22/-0.28/-0.44/+0.19, se=0.00621-0.00632
    A 20-seed sweep (seeds 1000-1019): mean z long -0.30 (max |z| 1.89), short +0.21 (max |z|
    2.32). The stop-out is dated at the END of its grid step (bias bound `ratio * |level| * r *
    dt / 2 * P(hit)`, <= ~0.24 SE at 20k paths -- why the path count is fixed here).
    tol_abs: long 0.009, short 0.0075.

    COVERAGE CAVEAT: blind to per-column discount differentiation for the same magnitude reason
    as G-MF1 (the discount-all-at-T mutant only re-dates the stopped-out residual). G-MF1's
    measured shift (long q=0 mean z -0.12 -> -0.68) is quoted for scale; this gate's own
    mutant was not separately measured (its unmutated mean z is the -0.30 / +0.21 above).
    G-RC1 pins discounting.
    """
    params = _mf_params(0.0)
    reset_times = tuple(0.08 * k for k in range(1, 13))
    note = _mf_term_sheet(side, spread=0.0, reset_times=reset_times)
    bundle = simulate(params, _engine(_N_STEPS), PseudoRandomSource(seed=seed))
    result = price_from_bundle(note, bundle, r=_MF_R)

    sign = 1.0 if side == "long" else -1.0
    reference = _MF_RATIO * sign * (params.s0 - note.financing_level)

    tol_abs = 0.009 if side == "long" else 0.0075
    assert 0.0 < result.std_err < tol_abs, (
        f"{side} seed={seed}: se={result.std_err} is not tight enough to be a meaningful gate "
        f"(tol_abs={tol_abs})"
    )
    assert abs(result.pv - reference) < 3.0 * result.std_err, (
        f"{side} seed={seed}: pv={result.pv}, ref={reference}, off by "
        f"{(result.pv - reference) / result.std_err:.2f} SE"
    )


_LSM_STEPS = 50
_LSM_EXERCISE_IDX = np.arange(1, _LSM_STEPS, dtype=np.intp)


def _lsm_engine(n_paths: int = 50_000) -> EngineConfig:
    return EngineConfig(
        scheme="qe", n_steps=_LSM_STEPS, n_paths=n_paths, expiry=1.0, antithetic=True
    )


def _american(
    option_type: Literal["call", "put"], strike: float, ratio: float, underlying: str = "SYN"
) -> Warrant:
    return Warrant(
        underlying=underlying,
        option_type=option_type,
        strike=strike,
        expiry=1.0,
        ratio=ratio,
        exercise="american",
    )


@pytest.mark.parametrize("model", ["aapl", "degenerate"])
def test_g_w2_zero_dividend_american_call_equals_european_twin_exactly(model: str) -> None:
    """G-W2. With q = 0 an American call is never exercised early (Merton), and the Jensen
    continuation floor makes the fitted policy reproduce that exactly: per-path
    `outcome.discounted` equals the European twin's discounted ledger to 1e-12 and no path
    exercises early. `[models.AAPL]` with q overridden to 0 (r=0.0425, Feller 0.333) and its
    BS-degenerate twin (sigma 0.2, same r, q=0), call struck at s0, ratio 0.1, QE / 50 steps /
    50k valuation paths + a disjoint 50k pilot.

    WHAT THIS PINS: the Jensen floor and the terminal-leg plumbing (discounting, `terminal_idx`,
    `held` bookkeeping). It does NOT pin the regression: with the floor on, the regression never
    gets to decide anything at q = 0, whatever it returns (G-W3 and G-W4 pin the regression).
    Measured: n_early_exercised = 0 on both models; max |discounted - european| = 0.0. Floor
    mutant (floor removed from fit and apply, same seeds): 715 (AAPL q=0) / 1,558 (degenerate) of
    50,000 paths exercise early, max |discounted - european| = 12.13 / 3.41. (The spec's 5,657 /
    z = -4.9 for the unfloored case was NOT reproduced; the mutant is sharp regardless.)
    """
    params = (
        _AAPL.model_copy(update={"q": 0.0})
        if model == "aapl"
        else HestonParams(
            s0=100.0,
            r=0.0425,
            q=0.0,
            v0=_SIGMA_DEGENERATE**2,
            kappa=1.5,
            theta=_SIGMA_DEGENERATE**2,
            xi=1e-4,
            rho=0.0,
        )
    )
    warrant = _american("call", params.s0, 0.1)
    pilot = simulate(params, _lsm_engine(), PseudoRandomSource(seed=501))
    policy = fit_exercise_policy(
        pilot,
        warrant.intrinsic,
        exercise_idx=_LSM_EXERCISE_IDX,
        terminal_idx=_LSM_STEPS,
        r=params.r,
        q=params.q,
    )
    del pilot
    bundle = simulate(params, _lsm_engine(), PseudoRandomSource(seed=502))
    outcome = apply_exercise_policy(bundle, warrant.intrinsic, policy, r=params.r, q=params.q)

    twin = Warrant(
        underlying=warrant.underlying,
        option_type="call",
        strike=warrant.strike,
        expiry=1.0,
        ratio=0.1,
        exercise="european",
    )
    european = discount(twin.cashflows(bundle, r=params.r), r=params.r)

    n_early = int(np.count_nonzero(outcome.exercise_step != _LSM_STEPS))
    assert n_early == 0, f"{n_early} paths exercised a zero-dividend call early"
    np.testing.assert_allclose(outcome.discounted, european, rtol=0.0, atol=1e-12)


def _lsm_degenerate(r: float, q: float) -> HestonParams:
    return HestonParams(
        s0=100.0,
        r=r,
        q=q,
        v0=_SIGMA_DEGENERATE**2,
        kappa=1.5,
        theta=_SIGMA_DEGENERATE**2,
        xi=1e-4,
        rho=0.0,
    )


@pytest.mark.parametrize("seed", [42, 7, 123, 2024])
@pytest.mark.parametrize(("option_type", "r", "q"), [("put", 0.06, 0.0), ("call", 0.02, 0.08)])
def test_g_w3_lsm_is_a_low_biased_estimate_of_the_bermudan_binomial_reference(
    option_type: Literal["call", "put"], r: float, q: float, seed: int
) -> None:
    """G-W3. BS-degenerate model, S0 = K = 100, sigma 0.2, T = 1, 50 steps, ratio 1; (a) put r=0.06
    q=0, (b) call r=0.02 q=0.08 (early exercise matters in both). 50k valuation paths + a disjoint
    50k pilot (pilot seed = seed + 10_000) through the real `price_american`. Reference:
    `crr_price(N=10000, exercise_every=200)` -- a BERMUDAN tree on exactly the 50 LSM exercise
    dates (the continuous-American value is ~0.014 higher, ~0.7 SE, and would be misread as LSM
    bias).

    ASSERTION STRUCTURE (ADR-006 A7 s3), exactly three conjuncts: `0 < se < tol_abs`;
    `pv - ref < 3 se` (UPPER bound: an out-of-sample fixed policy cannot beat the true Bermudan
    value, so this is valid at any path count); and
    `ref - pv < 3 se + 0.05` (a DECLARED low-bias budget: the LSM estimate is low by a
    basis-limited amount that does not shrink with N, so a two-sided 3-SE gate is FORBIDDEN -- it
    would go red as N grows). Sharp against never-exercising (bias -0.57). NOT sharp against
    in-sample fitting: fitting and valuing on the SAME bundle (6 coefficients, 50k paths) measured
    put -0.036/-0.005/+0.025/-0.028 and call -0.048/-0.022/+0.045/-0.049 at seeds 42/7/123/2024,
    i.e. only ~+0.01 (~0.5 SE) above the out-of-sample figures below -- far inside the 3-SE upper
    bound. That defect is blocked structurally by `price_american`'s disjoint-seed guard
    (`test_lsm.py`), not by this gate.

    Measured (ref put 5.78514, call 5.72492; se put 0.0193-0.0194, call 0.0260-0.0264):
        put  seed 42/7/123/2024: bias -0.0492/-0.0068/+0.0165/-0.0468
        call seed 42/7/123/2024: bias -0.0666/-0.0314/+0.0143/-0.0570
    20-seed sweep (seeds 500..519), bias = pv - ref: put mean -0.0293 (range -0.0674..+0.0117),
    call mean -0.0435 (range -0.0814..-0.0048); max |bias/se| 3.45 put / 3.09 call -- i.e. a
    two-sided 3-SE gate would fail on ~1 seed in 20. At 400k valuation paths the same policies
    give put -0.011 / call -0.025 (3 seeds, pilot 50k and 200k alike): basis-limited, not
    pilot-limited. tol_abs: put 0.021, call 0.028 (just above the measured SEs).
    """
    params = _lsm_degenerate(r, q)
    warrant = _american(option_type, 100.0, 1.0)
    result = price_american(
        warrant,
        params,
        _lsm_engine(),
        PseudoRandomSource(seed=seed),
        pilot_rng=PseudoRandomSource(seed=seed + 10_000),
    )
    reference = crr_price(
        s0=100.0,
        strike=100.0,
        r=r,
        q=q,
        sigma=_SIGMA_DEGENERATE,
        expiry=1.0,
        option_type=option_type,
        n_tree_steps=10_000,
        exercise_every=200,
    )
    pv, se = result.price.pv, result.price.std_err

    tol_abs = 0.021 if option_type == "put" else 0.028
    assert 0.0 < se < tol_abs, (
        f"{option_type} seed={seed}: se={se} is not tight enough to be a meaningful gate "
        f"(tol_abs={tol_abs})"
    )
    assert pv - reference < 3.0 * se, (
        f"{option_type} seed={seed}: pv={pv} EXCEEDS the Bermudan reference {reference} by "
        f"{(pv - reference) / se:.2f} SE -- an out-of-sample policy cannot do that"
    )
    assert reference - pv < 3.0 * se + 0.05, (
        f"{option_type} seed={seed}: pv={pv} is {reference - pv:.4f} below ref={reference}, beyond "
        "the 3 SE + 0.05 declared low-bias budget"
    )


def _s_only_basis(x: NDArray[np.float64], v: NDArray[np.float64]) -> NDArray[np.float64]:
    """`[1, x, x^2]` -- the S-only basis ADR-006 A3 s1 calls a defect under stochastic vol."""
    return np.column_stack([np.ones_like(x), x, x * x])


@pytest.mark.parametrize("seed", [42, 7, 123, 2024])
def test_g_w4_sv_basis_beats_s_only_basis_under_heston(seed: int) -> None:
    """G-W4. `[models.SPX]` (Feller 0.231), American put K = s0, ratio 0.01, QE / 50 steps, 50k
    valuation paths + a disjoint 50k pilot (pilot seed = seed + 10_000). Policy A (the (S, v)
    `quadratic_sv_basis`) vs policy B (a test-local S-only `[1, x, x^2]`), fitted on the SAME pilot
    bundle and applied to the SAME valuation bundle, so the per-path difference carries almost no
    sampling noise. Asserts `mean(A - B) > 3 * se_diff` with `se_diff` the antithetic pair-mean SE
    of the paired difference. Sharp against regressing on S alone (exo/CLAUDE.md M2 rule).

    Measured (diff = A - B, se_diff):
        seed 42:   A=2.87276 B=2.81605 diff=0.05671 se_diff=0.01163 z=4.88
        seed 7:    A=2.87213 B=2.78059 diff=0.09153 se_diff=0.01198 z=7.64
        seed 123:  A=2.84267 B=2.78050 diff=0.06217 se_diff=0.01163 z=5.35
        seed 2024: A=2.88036 B=2.83239 diff=0.04797 se_diff=0.01196 z=4.01
    20-seed sweep (seeds 500..519): mean z 6.38, min z 3.51, max z 8.85, mean diff 0.0788,
    se_diff 0.0117-0.0128.
    """
    warrant = _american("put", _SPX.s0, 0.01, "SPX")
    kwargs = {
        "exercise_idx": _LSM_EXERCISE_IDX,
        "terminal_idx": _LSM_STEPS,
        "r": _SPX.r,
        "q": _SPX.q,
    }
    pilot = simulate(_SPX, _lsm_engine(), PseudoRandomSource(seed=seed + 10_000))
    policy_a = fit_exercise_policy(pilot, warrant.intrinsic, **kwargs)
    policy_b = fit_exercise_policy(pilot, warrant.intrinsic, basis=_s_only_basis, **kwargs)
    del pilot
    bundle = simulate(_SPX, _lsm_engine(), PseudoRandomSource(seed=seed))
    a = apply_exercise_policy(bundle, warrant.intrinsic, policy_a, r=_SPX.r, q=_SPX.q)
    b = apply_exercise_policy(bundle, warrant.intrinsic, policy_b, r=_SPX.r, q=_SPX.q)
    paired = mc_estimate(bundle, a.discounted - b.discounted)

    assert paired.pv > 3.0 * paired.std_err, (
        f"seed={seed}: mean(A - B)={paired.pv} is only {paired.pv / paired.std_err:.2f} "
        "paired SE above zero -- the (S, v) basis shows no advantage over S-only"
    )
