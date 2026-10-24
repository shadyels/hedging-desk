"""P2.M3 BLOCKING validation gates for TARF / PTARF and the EURUSD warrant (exo/CLAUDE.md: "MC vs
closed-form agreement within 3 standard errors is the acceptance test"). FX rides `HestonParams`
with `r := r_d` (USD) and `q := r_f` (EUR) (ADR-006 A8 s1).

- G-T1: TARF with an unreachable target (G = 1e15) is a strip of 12 monthly forwards-with-
  leverage, PV = `N * sum_j [call_j - L * put_j]` against the Heston CF vanilla, on
  `[models.EURUSD]` (Feller-violating, ratio 0.389). The target never binds, so this isolates
  the payoff and the per-column discounting from the knock-out state loop.
- G-T1b: the same term sheet on the BS-degenerate FX model vs Garman-Kohlhagen (`bs_*_price`).
- G-T2a / G-T2b: exact knock-out identities. 2a: first fixing always breaches -> `[G, 0, ...]` on
  every path, PV = `G * exp(-r_d/12)` (se float noise). 2b: the paths that breach at fixing 1
  have rows exactly `[G, 0, ..., 0]`.
- G-T3: TARF with K = 0.54 (far below every simulated spot), L = 1, G = 1e15: every column is a
  non-zero forward `N * (S_j - K)` on every path, PV = `N * sum_j (S0 e^{-r_f t_j} - K e^{-r_d
  t_j})`. A6 section 9 framing: `discount()` itself is pinned by G-RC1
  (`test_validation_gates_equity.py`); G-T3 pins the Tarf fixing->column->date mapping and the
  r_d/r_f assignment on random non-zero columns.
- G-T4a / G-T4b: PTARF reductions. 4a: pivot far above every spot -> PTARF == TARF per path
  (exact). 4b: pivot below every spot -> `N * (K_hi - S_j)` per path.
- G-T5: EURUSD European warrant (call and put, 3 strikes, T = 1/12 and T = 1) vs the Heston CF
  vanilla; checks QE at FX vol at T = 1/12 (4 steps) and T = 1 (see the open observation and
  the marker above `_engine`).

EVERY 3-SE GATE CARRIES A SECOND CONJUNCT BOUNDING THE SE ITSELF (`0 < se < tol_abs`, see
`test_validation_gates_equity.py`): `tol_abs` is set just above the largest SE of a 20-seed sweep
(seeds 500..519) at each gate's n_paths/n_steps, recorded in each docstring. Gate seeds 601-606.
"""

from __future__ import annotations

import functools
import math
import tomllib
from pathlib import Path
from typing import Literal

import numpy as np
import pytest
from numpy.testing import assert_allclose, assert_array_equal

from exo.models.analytic import bs_call_price, bs_put_price
from exo.models.heston import PathBundle, simulate
from exo.models.heston_cf import heston_call_price_and_error, heston_vanilla_price
from exo.models.params import EngineConfig, HestonParams, load_models_section
from exo.models.rng import PseudoRandomSource
from exo.products.base import observation_indices
from exo.products.pricer import price_from_bundle
from exo.products.tarf import Ptarf, Tarf
from exo.products.warrant import Warrant

_EURUSD = load_models_section(
    tomllib.loads((Path(__file__).parents[1] / "exo.toml").read_text())["models"]
)[1]["EURUSD"]

_SIGMA_FX = 0.09
_DEGENERATE_FX = HestonParams(
    s0=1.08,
    r=0.0425,
    q=0.02,
    v0=_SIGMA_FX**2,
    kappa=1.5,
    theta=_SIGMA_FX**2,
    xi=1e-4,
    rho=0.0,
)

_FIX = tuple(i / 12 for i in range(1, 13))
_N = 1e6
_HUGE_G = 1e15


# ponytail: QE at 4 steps per monthly fixing (n_steps=4 for T=1/12). Ceiling: an observed mean z of
# about +0.24 on the ATM put over seeds 700-899 (about 3.6 SE), a possible small discretisation
# bias that the single-seed 3-SE gate cannot resolve. Trigger: a finer-grid check (T=1/12 at 4 vs
# 16+ steps, many seeds) showing a bias above noise, or any G-T5 cell failing at a tighter
# tolerance. See docs/PONYTAIL-DEBT.md.
def _engine(n_steps: int = 48, expiry: float = 1.0, n_paths: int = 20_000) -> EngineConfig:
    return EngineConfig(
        scheme="qe", n_steps=n_steps, n_paths=n_paths, expiry=expiry, antithetic=True
    )


def _bundle(params: HestonParams, seed: int) -> PathBundle:
    return simulate(params, _engine(), PseudoRandomSource(seed=seed))


def _tarf(strike: float, leverage: float, target: float) -> Tarf:
    return Tarf("EURUSD", _N, strike, leverage, target, _FIX, 1.0)


def _fix_idx(bundle: PathBundle) -> np.ndarray:
    return observation_indices(np.asarray(_FIX, dtype=np.float64), bundle)


# --- G-T1 -----------------------------------------------------------------------------------


@functools.cache
def _t1_reference() -> float:
    return _N * sum(
        heston_vanilla_price(_EURUSD, 1.08, t, True)
        - 2.0 * heston_vanilla_price(_EURUSD, 1.08, t, False)
        for t in _FIX
    )


def test_g_t1_unreachable_target_tarf_matches_heston_strip_within_3se() -> None:
    """G-T1. `[models.EURUSD]`, Tarf(K=1.08, L=2, G=1e15, N=1e6), 12 monthly fixings. The target
    can never bind (precondition asserted: the largest per-path sum of positive amounts is
    below G), so PV = `N * sum_j [call(K, t_j) - 2 * put(K, t_j)]` against `heston_vanilla_price`.
    Also asserts the CF quadrature's own error estimate (`abserr < 1e-10`) at every fixing.

    Measured at n_steps=48, n_paths=20_000, ref=-75,014.95: gate seed=601 pv=-77,513.81
    se=5,230.58 z=-0.478. 20-seed sweep (seeds 500..519), z per seed: -1.62 +0.17 +0.29 -1.21
    -0.77 -1.53 -0.26 -0.12 -0.32 -0.34 -1.39 -0.01 -0.78 -1.21 +1.36 +1.54 +0.22 +0.52 +1.02
    -0.04; mean z -0.22, max|z| 1.62, SE 5,141.6..5,324.6. tol_abs=5500 sits just above the max SE.
    """
    for t in _FIX:
        _, abserr = heston_call_price_and_error(_EURUSD, 1.08, t)
        assert abserr < 1e-10, f"CF reference not converged at t={t}: abserr={abserr}"

    bundle = _bundle(_EURUSD, 601)
    ledger = _tarf(1.08, 2.0, _HUGE_G).cashflows(bundle, r=_EURUSD.r)
    assert np.clip(ledger.amounts, 0.0, None).sum(axis=1).max() < _HUGE_G
    result = price_from_bundle(_tarf(1.08, 2.0, _HUGE_G), bundle, r=_EURUSD.r)

    reference = _t1_reference()
    tol_abs = 5500.0
    assert 0.0 < result.std_err < tol_abs, (
        f"se={result.std_err} is not tight enough to be a meaningful gate (tol_abs={tol_abs})"
    )
    assert abs(result.pv - reference) < 3.0 * result.std_err, (
        f"pv={result.pv}, ref={reference}, off by {(result.pv - reference) / result.std_err:.2f} SE"
    )


# --- G-T1b ----------------------------------------------------------------------------------


@functools.cache
def _t1b_reference() -> float:
    kw = {"s0": 1.08, "r": 0.0425, "q": 0.02, "sigma": _SIGMA_FX}
    return _N * sum(
        bs_call_price(strike=1.08, expiry=t, **kw) - 2.0 * bs_put_price(strike=1.08, expiry=t, **kw)
        for t in _FIX
    )


def _t1b_run(seed: int) -> tuple[float, float]:
    bundle = _bundle(_DEGENERATE_FX, seed)
    result = price_from_bundle(_tarf(1.08, 2.0, _HUGE_G), bundle, r=_DEGENERATE_FX.r)
    return result.pv, result.std_err


def test_g_t1b_unreachable_target_tarf_matches_garman_kohlhagen_within_3se() -> None:
    """G-T1b. As G-T1 on the BS-degenerate FX model (sigma 0.09, xi = 1e-4) against
    Garman-Kohlhagen (`bs_call_price` / `bs_put_price` with q = r_f). Bias-free at any path
    count, so a discrepancy is a payoff/discounting defect, not model error.

    Measured at n_steps=48, n_paths=20_000, ref=-95,404.77: gate seed=602 pv=-98,087.20
    se=1,381.59 z=-1.942. 20-seed sweep (seeds 500..519), z per seed: -0.30 -1.10 +0.11 +0.01
    +0.60 +2.12 +1.12 -0.94 -0.02 +2.12 -1.24 +1.24 +0.08 -0.94 -0.07 +1.56 +0.32 -0.32 +1.43
    -2.17; mean z +0.18, max|z| 2.17, SE 1,352.5..1,393.1. tol_abs=1450 sits just above the max SE.

    Mutant check (scratch, not in the file; same bundle, z vs this reference): leverage moved to
    the GAIN side (`2*gain - loss`) gives z = +280.7 (seed 602; +279.7 / +279.6 at seeds
    500 / 501); the literal "leverage on both sides" (`2*gain - 2*loss`) gives z = +597.5;
    dropping leverage (`gain - loss`) gives z = +737.8.
    """
    result_pv, result_se = _t1b_run(602)
    reference = _t1b_reference()
    tol_abs = 1450.0
    assert 0.0 < result_se < tol_abs, (
        f"se={result_se} is not tight enough to be a meaningful gate (tol_abs={tol_abs})"
    )
    assert abs(result_pv - reference) < 3.0 * result_se, (
        f"pv={result_pv}, ref={reference}, off by {(result_pv - reference) / result_se:.2f} SE"
    )


# --- G-T2a / G-T2b --------------------------------------------------------------------------


def test_g_t2a_first_fixing_always_breaches_is_exact() -> None:
    """G-T2a. `[models.EURUSD]`, Tarf(K=0.01, L=2, G=1.0, N=1e6). Precondition (asserted): every
    path has `N * (S_1 - K) > G` at the first fixing, so every path pays exactly `[G, 0, ..., 0]`
    and PV = `G * exp(-r_d / 12)` = 0.9964645976 with se equal to float noise. Pins the
    knock-out identity and the zeroing after knock-out (the capped-remainder cap is pinned by U1
    in `test_tarf.py`: at the first fixing acc = 0, so target - acc equals target).

    Gate seed 603.
    """
    bundle = _bundle(_EURUSD, 603)
    idx = _fix_idx(bundle)
    assert bundle.S[:, idx[0]].min() > 0.01 + 1.0 / _N, "precondition: first fixing must breach"

    tarf = _tarf(0.01, 2.0, 1.0)
    amounts = tarf.cashflows(bundle, r=_EURUSD.r).amounts
    assert_array_equal(amounts[:, 0], np.full(amounts.shape[0], 1.0))
    assert_array_equal(amounts[:, 1:], np.zeros((amounts.shape[0], 11)))

    result = price_from_bundle(tarf, bundle, r=_EURUSD.r)
    reference = 1.0 * math.exp(-_EURUSD.r / 12.0)
    assert result.std_err <= 1e-12, f"se={result.std_err} must be float noise"
    assert abs(result.pv - reference) <= 1e-12, f"pv={result.pv}, ref={reference}"


def test_g_t2b_paths_breaching_at_first_fixing_pay_exactly_g_then_zero() -> None:
    """G-T2b. `[models.EURUSD]`, Tarf(K=1.08, L=2, G=2000, N=1e6). Rows with `N * (S_1 - K) >=
    G` (the `>=` breach rule) equal `[G, 0, ..., 0]` EXACTLY; the knock-out set is asserted
    non-empty and not every row, so both branches of the state loop are exercised. Pins the
    knock-out identity and zeroing after knock-out (the cap is pinned by U1 in `test_tarf.py`).

    Gate seed 603.
    """
    bundle = _bundle(_EURUSD, 603)
    idx = _fix_idx(bundle)
    amounts = _tarf(1.08, 2.0, 2000.0).cashflows(bundle, r=_EURUSD.r).amounts

    ko = _N * (bundle.S[:, idx[0]] - 1.08) >= 2000.0
    assert 0 < int(ko.sum()) < ko.size, f"knock-out set must be a proper subset, got {ko.sum()}"
    expected = np.zeros((int(ko.sum()), 12))
    expected[:, 0] = 2000.0
    assert_array_equal(amounts[ko], expected)
    assert (amounts[~ko, 0] != 2000.0).all()


# --- G-T3 -----------------------------------------------------------------------------------


@functools.cache
def _t3_reference() -> float:
    r_d, r_f = _DEGENERATE_FX.r, _DEGENERATE_FX.q
    return _N * sum(1.08 * math.exp(-r_f * t) - 0.54 * math.exp(-r_d * t) for t in _FIX)


def test_g_t3_deep_itm_tarf_pins_fixing_column_date_mapping_within_3se() -> None:
    """G-T3. BS-degenerate FX model, Tarf(K=0.54, L=1, G=1e15, N=1e6): K is far below every
    simulated spot, so every column is the non-zero forward `N * (S_j - K)` on every path
    (asserted), and PV = `N * sum_j (S0 e^{-r_f t_j} - K e^{-r_d t_j})` with r_f = q = 0.02,
    r_d = r = 0.0425 (6,487,567.6956).

    A6 section 9 framing: `discount()` itself is pinned by G-RC1
    (`test_validation_gates_equity.py`); G-T3 pins the Tarf fixing->column->date mapping and the
    r_d/r_f assignment on random non-zero columns.

    Measured at n_steps=48, n_paths=20_000, ref=6,487,567.6956: gate seed=604 pv=6,487,492.62
    se=316.96 z=-0.237. 20-seed sweep (seeds 500..519), z per seed: +1.11 +1.43 +0.08 -0.13
    -0.48 -1.94 -1.17 +0.85 -0.14 -1.50 +0.37 -1.24 -0.14 +1.53 -0.32 -0.92 -0.35 -0.53 -1.69
    +1.89; mean z -0.16, max|z| 1.94, SE 314.4..336.8. tol_abs=350 sits just above the max SE.

    Mutants (scratch, not in the file; z at seed 604, 500 and 501 respectively): discounting every
    column at T = 1 instead of t_j: -398.0 / -378.3 / -373.0; discounting at t_j + 1/12 (shifted
    one fixing): -72.9 / -68.2 / -66.9 (t_j - 1/12: +72.1 / +70.1 / +69.5); discounting at r_f
    instead of r_d: +247.2 / +237.1 / +234.3.
    """
    bundle = _bundle(_DEGENERATE_FX, 604)
    amounts = _tarf(0.54, 1.0, _HUGE_G).cashflows(bundle, r=_DEGENERATE_FX.r).amounts
    assert (amounts != 0.0).all(), "every column must be non-zero on every path"
    result = price_from_bundle(_tarf(0.54, 1.0, _HUGE_G), bundle, r=_DEGENERATE_FX.r)
    result_pv, result_se = result.pv, result.std_err

    reference = _t3_reference()
    tol_abs = 350.0
    assert 0.0 < result_se < tol_abs, (
        f"se={result_se} is not tight enough to be a meaningful gate (tol_abs={tol_abs})"
    )
    assert abs(result_pv - reference) < 3.0 * result_se, (
        f"pv={result_pv}, ref={reference}, off by {(result_pv - reference) / result_se:.2f} SE"
    )


# --- G-T4a / G-T4b --------------------------------------------------------------------------


def test_g_t4a_ptarf_with_pivot_above_every_spot_equals_tarf_per_path() -> None:
    """G-T4a. `[models.EURUSD]`, Ptarf(strike_low=1.08, pivot=1e3, strike_high=2e3, L=2,
    G=50_000, N=1e6) vs Tarf(K=1.08, L=2, G=50_000, N=1e6). Precondition (asserted): every
    fixing spot is below the pivot, so the lower regime applies everywhere and the two ledgers
    on the SAME bundle are identical to the bit.

    Gate seed 605.
    """
    bundle = _bundle(_EURUSD, 605)
    assert bundle.S[:, _fix_idx(bundle)].max() < 1e3, "precondition: spot below the pivot"

    ptarf = Ptarf("EURUSD", _N, 1.08, 1e3, 2e3, 2.0, 50_000.0, _FIX, 1.0)
    tarf = Tarf("EURUSD", _N, 1.08, 2.0, 50_000.0, _FIX, 1.0)
    assert_array_equal(
        ptarf.cashflows(bundle, r=_EURUSD.r).amounts, tarf.cashflows(bundle, r=_EURUSD.r).amounts
    )


def test_g_t4b_ptarf_with_pivot_below_every_spot_pays_upper_regime_forward() -> None:
    """G-T4b. `[models.EURUSD]`, Ptarf(strike_low=1e-3, pivot=2e-3, strike_high=1.08, L=1,
    G=1e15, N=1e6). Precondition (asserted): every fixing spot is at or above the pivot, so
    only the upper regime applies and gain minus loss is `N * (K_hi - S_j)` on every path and
    column (sign flips through K_hi with L = 1). Sharp against a swapped regime test or a gain/
    loss mix-up in the upper branch.

    Gate seed 605.
    """
    bundle = _bundle(_EURUSD, 605)
    s = bundle.S[:, _fix_idx(bundle)]
    assert s.min() >= 2e-3, "precondition: spot at or above the pivot"

    ptarf = Ptarf("EURUSD", _N, 1e-3, 2e-3, 1.08, 1.0, _HUGE_G, _FIX, 1.0)
    amounts = ptarf.cashflows(bundle, r=_EURUSD.r).amounts
    assert_allclose(amounts, _N * (1.08 - s), rtol=1e-12, atol=1e-6)


# --- G-T5 -----------------------------------------------------------------------------------


def _t5_cell(
    option_type: Literal["call", "put"], moneyness: float, expiry: float, seed: int
) -> tuple[float, float, float]:
    n_steps = 4 if expiry < 1.0 else 48
    strike = moneyness * _EURUSD.s0
    warrant = Warrant(
        underlying="EURUSD",
        option_type=option_type,
        strike=strike,
        expiry=expiry,
        ratio=1.0,
        exercise="european",
    )
    bundle = simulate(_EURUSD, _engine(n_steps, expiry), PseudoRandomSource(seed=seed))
    result = price_from_bundle(warrant, bundle, r=_EURUSD.r)
    reference = heston_vanilla_price(
        _EURUSD, strike=strike, expiry=expiry, is_call=option_type == "call"
    )
    return result.pv, result.std_err, reference


@pytest.mark.parametrize("expiry", [1.0 / 12.0, 1.0])
@pytest.mark.parametrize("option_type", ["call", "put"])
@pytest.mark.parametrize("moneyness", [0.95, 1.0, 1.05])
def test_g_t5_eurusd_european_warrant_matches_heston_vanilla_within_3se(
    option_type: Literal["call", "put"], moneyness: float, expiry: float
) -> None:
    """G-T5. `[models.EURUSD]` (Feller ratio 0.389), European warrant ratio 1, K in {0.95, 1.0,
    1.05} * s0, call and put, T = 1/12 (4 steps) and T = 1 (48 steps), QE, 20k paths, vs
    `heston_vanilla_price`. Also asserts the CF quadrature's `abserr < 1e-10`. Checks QE at FX
    vol at T = 1/12 (4 steps) and T = 1; see the open observation and the marker above `_engine`.

    Gate seed 606, one bundle per (T, seed) shared by all six cells of that T. Gate-seed z
    at T=1/12 (calls 0.95/1.0/1.05, puts 0.95/1.0/1.05): +1.78 +1.46 +0.59 +0.89 +0.29 -1.23; at
    T=1: -1.98 -1.02 -0.35 +1.16 +2.19 +2.58. 20-seed sweep (seeds 500..519), per cell: mean z /
    max|z| / max SE:
        T=1/12 call 0.95: -0.12 / 1.80 / 6.69e-5    put 0.95: -0.17 / 2.82 / 2.39e-5
        T=1/12 call 1.00: +0.06 / 2.03 / 8.48e-5    put 1.00: +0.10 / 2.25 / 9.29e-5
        T=1/12 call 1.05: -0.34 / 1.70 / 2.20e-5    put 1.05: -0.03 / 1.63 / 7.04e-5
        T=1    call 0.95: -0.29 / 2.37 / 4.00e-4    put 0.95: -0.09 / 2.93 / 2.54e-4
        T=1    call 1.00: -0.28 / 2.92 / 3.62e-4    put 1.00: -0.01 / 2.80 / 3.32e-4
        T=1    call 1.05: -0.21 / 3.13 / 3.13e-4    put 1.05: +0.09 / 2.53 / 3.82e-4
    tol_abs 1.0e-4 (T=1/12) / 4.5e-4 (T=1) sit above the max SE.

    FLAG, resolved: one cell-seed (T=1 call 1.05, seeds 500..519) has |z| 3.13 > 3 (expected
    count over 240 cell-seeds under a correct estimator ~0.65). Targeted re-sweep of that cell,
    seeds 700..899: 2 of 200 above |z| 3, 11 above |z| 2, mean z -0.029 (SE 0.076), sd z 1.08,
    max |z| 3.55 at seed 714. No bias detected; the tail is consistent with noise at the 0.4%
    level. All 12 cells on seeds 700..899: 6 of 2,400 cell-seeds above |z| 3 (0.25%).

    OPEN OBSERVATION (not a gate failure): over seeds 700..899 the T=1/12 cells (n_steps=4)
    have mean z from -0.13 to +0.24 across cells (SE ~0.07 each); the ATM put is +0.24 (~3.6
    SE). The gate's single-seed 3-SE band cannot detect a small discretisation bias at 4 steps.
    Undecided whether a finer-grid check is needed.
    """
    strike = moneyness * _EURUSD.s0
    _, abserr = heston_call_price_and_error(_EURUSD, strike, expiry)
    assert abserr < 1e-10, f"CF reference not converged: abserr={abserr}"

    pv, se, reference = _t5_cell(option_type, moneyness, expiry, 606)
    tol_abs = 1.0e-4 if expiry < 1.0 else 4.5e-4
    assert 0.0 < se < tol_abs, (
        f"{option_type} {moneyness} T={expiry}: se={se} is not tight enough to be a meaningful "
        f"gate (tol_abs={tol_abs})"
    )
    assert abs(pv - reference) < 3.0 * se, (
        f"{option_type} {moneyness} T={expiry}: pv={pv}, ref={reference}, off by "
        f"{(pv - reference) / se:.2f} SE"
    )
