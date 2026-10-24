"""Blocking validation gates for the CRN bump-and-revalue Greeks (ADR-006 Amendment 9 s3).

Fixture: a European warrant, ratio 1, K = S0 = 100, T = 1, on the BS-degenerate Heston set (sigma
0.2, r 0.02, q 0.01, xi 1e-4, kappa 1.5, rho 0), QE, 50 steps, 40,000 antithetic paths,
PRODUCTION bump sizes (`exo.toml [greeks]`: spot 1% relative, vol 0.005, rate 0.001, yield 0.001,
theta over 365 days). The fixture is deliberately degenerate so a closed form exists. It pins the
estimator machinery -- scaling, leg pairing, joint v0/theta vega, rho moving drift and discount,
ageing through `DropLeadingStepsSource` -- and says NOTHING about SE at the published book's
configuration (ADR-006 Amendment 9 s7 measures that; `test_book_storyline_delta.py` pins it).

The reference is the SAME finite difference of `bs_call_price`/`bs_put_price` the estimator takes
(`tests/_greeks_gate_helpers.py::references`), not the analytic BS Greek: the estimator is unbiased
for the finite difference, so this removes bump bias from the comparison by construction.

IMPLEMENTATION'S OWN 20-SEED SWEEP (seeds 500-519, this configuration, 40 `compute_greeks` runs):
- max |z| over all 240 draws (6 Greeks x 2 options x 20 seeds): 2.37 (put theta); 2.35 (call
  theta). The expected maximum of 240 independent |N(0, 1)| draws is about 2.87.
- mean z per Greek, call / put: delta -0.03 / -0.02; gamma -0.01 / -0.01; vega -0.14 / -0.24;
  rho +0.10 / +0.10; dq +0.02 / +0.00; theta +0.34 / +0.28.
- max |z| per Greek, call / put: delta 1.66 / 1.72; gamma 1.80 / 1.80; vega 1.52 / 1.49;
  rho 1.63 / 1.63; dq 1.75 / 1.71; theta 2.35 / 2.37.
- max SE per Greek, call / put: delta 8.72e-4 / 7.49e-4; gamma 5.61e-4 / 5.61e-4; vega 0.318 /
  0.126; rho 0.0686 / 0.0686; dq 0.0956 / 0.0846; theta 1.557e-3 / 1.103e-3. `_TOL_ABS` below is
  1.1 x these, so the `0 < se < tol_abs` conjunct fails only if the estimator gets noisier.
- G-CRN minimum se_indep / se_crn over the sweep, call / put: delta 42.2 / 31.6; vega 23.2 / 37.4;
  rho 539 / 347; theta 6.43 / 5.85. Thresholds (10, 10, 100, 3) sit below all of them.
- Theta with the same seed but NO dropped leading step (the aged run consumes shifted increments,
  i.e. effectively uncoupled): SE is 6.48 / 5.89 x larger than the coupled estimator's at minimum.
  An uncoupled theta PASSES the 3-SE z-test on its own (its SE is just wider); only the ratio
  checks below catch it.
"""

from __future__ import annotations

import functools
import math

import pytest
from _greeks_gate_helpers import (
    CRN_GREEKS,
    GREEK_NAMES,
    Opt,
    references,
    run_greeks,
    se_independent,
    se_theta_undropped,
    zscores,
)

from exo.greeks.bump import Estimate, Greeks

_SEEDS: dict[Opt, int] = {"call": 701, "put": 702}

# 1.1 x the largest SE of the 20-seed sweep above.
_TOL_ABS: dict[Opt, dict[str, float]] = {
    "call": {
        "delta": 9.589e-4, "gamma": 6.171e-4, "vega": 0.3494, "rho": 0.07548, "dq": 0.1052,
        "theta": 1.713e-3,
    },
    "put": {
        "delta": 8.239e-4, "gamma": 6.171e-4, "vega": 0.1387, "rho": 0.07548, "dq": 0.09301,
        "theta": 1.213e-3,
    },
}  # fmt: skip
_MIN_RATIO = {"delta": 10.0, "vega": 10.0, "rho": 100.0, "theta": 3.0}
# The sweep's largest ratio is 539 (rho). A ratio above this means the CRN SE has collapsed
# (everything cancels: a degenerate estimator that "passes" trivially), which must raise, not pass.
_MAX_PLAUSIBLE_RATIO = 1e5


def _crn_ratio(option: str, name: str, se_crn: float, se_indep: float) -> float:
    """se_indep / se_crn, raising on every degenerate input (zero, NaN, inf, or an implausibly
    large ratio) so that a broken criterion cannot pass by accident (ADR-006 Amendment 9 s3)."""
    for label, se in (("crn", se_crn), ("indep", se_indep)):
        if not math.isfinite(se) or se <= 0.0:
            raise ValueError(f"{option} {name}: degenerate se ({label}={se})")
    ratio = se_indep / se_crn
    if ratio > _MAX_PLAUSIBLE_RATIO:
        raise ValueError(f"{option} {name}: implausible ratio {ratio:.3g}: CRN SE has collapsed")
    return ratio


@functools.cache
def _greeks(option: Opt) -> Greeks:
    return run_greeks(option, _SEEDS[option])


@functools.cache
def _indep(option: Opt) -> dict[str, float]:
    return se_independent(option, _SEEDS[option])


def _gate_gk(option: Opt, greeks: Greeks | None = None) -> None:
    greeks = _greeks(option) if greeks is None else greeks
    ref = references(option)
    for name in GREEK_NAMES:
        est: Estimate = getattr(greeks, name)
        tol_abs = _TOL_ABS[option][name]
        assert 0.0 < est.std_err < tol_abs, (
            f"{option} {name}: se={est.std_err} is not a meaningful gate (tol_abs={tol_abs})"
        )
        assert abs(est.value - ref[name]) < 3.0 * est.std_err, (
            f"{option} {name}: est={est.value} ref={ref[name]} se={est.std_err} "
            f"z={(est.value - ref[name]) / est.std_err:+.2f}"
        )


def test_g_gk1_call_greeks_match_same_bump_finite_difference() -> None:
    _gate_gk("call")


def test_g_gk2_put_greeks_match_same_bump_finite_difference() -> None:
    _gate_gk("put")


@pytest.mark.parametrize("option", ["call", "put"])
def test_g_gk_theta_is_resolvable_from_zero(option: Opt) -> None:
    """Sharpness: a theta stuck at 0 would sit at z about 8.3 (put) / 7.6 (call) against the honest
    SE, so the gate would fail it; an estimator with `se == 0` is rejected by `0 < se` above."""
    ref = references(option)["theta"]
    assert abs(0.0 - ref) > 3.0 * _greeks(option).theta.std_err


@pytest.mark.parametrize("option", ["call", "put"])
def test_g_crn_se_ratio_of_independent_to_common_random_numbers(option: Opt) -> None:
    crn, indep = _greeks(option), _indep(option)
    for name in CRN_GREEKS:
        ratio = _crn_ratio(option, name, getattr(crn, name).std_err, indep[name])
        assert ratio >= _MIN_RATIO[name], (
            f"{option} {name}: se_indep/se_crn = {ratio:.1f} < {_MIN_RATIO[name]}"
        )


@pytest.mark.parametrize(
    ("se_crn", "se_indep"),
    [
        (0.0, 1.0),  # ratio would be +inf: "everything passes" trivially
        (1.0, 0.0),
        (0.0, 0.0),  # 0/0
        (float("nan"), 1.0),
        (1.0, float("inf")),
        (1e-12, 1.0),  # finite but implausible: CRN cancelled everything
    ],
)
def test_g_crn_degenerate_se_raises_instead_of_passing(se_crn: float, se_indep: float) -> None:
    with pytest.raises(ValueError, match="degenerate|implausible"):
        _crn_ratio("call", "delta", se_crn, se_indep)


def test_g_crn_nothing_passes_is_a_failure_not_a_silent_fallback() -> None:
    """The other degenerate side: no CRN at all (ratio ~ 1) fails every threshold."""
    for name, floor in _MIN_RATIO.items():
        assert _crn_ratio("call", name, 1.0, 1.0) < floor


@pytest.mark.parametrize("option", ["call", "put"])
def test_g_crn_theta_coupling_comes_from_dropping_the_leading_step(option: Opt) -> None:
    """Without `DropLeadingStepsSource` the aged run is effectively independent of the base run
    and theta's SE inflates by the G-CRN factor (>= 3), though it would still pass the z-test."""
    undropped = se_theta_undropped(option, _SEEDS[option])
    assert undropped / _greeks(option).theta.std_err >= _MIN_RATIO["theta"]


def test_gate_helper_raises_on_zero_se() -> None:
    zero = Estimate(0.0, 0.0)
    ok = Estimate(1.0, 0.1)
    bad = Greeks(pv=ok, delta=ok, gamma=ok, vega=ok, rho=ok, dq=ok, theta=zero)
    with pytest.raises(ValueError, match="degenerate"):
        zscores("call", bad)
