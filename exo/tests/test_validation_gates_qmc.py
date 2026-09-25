"""P2.M1 slice 3's RQMC counterparts of slice 1/2's BLOCKING pseudo-random validation gates
(exo/CLAUDE.md: "MC vs closed-form agreement within 3 standard errors is the acceptance test";
ADR-006 Amendment 4 s4, Amendment 6 for RQMC).

THE RULE THIS FILE FOLLOWS: every `tol_abs` literal below is copied VERBATIM from the pseudo-
random gate it counterparts (`test_validation_gates.py` / `test_validation_gates_products.py`),
never re-measured for the Sobol sampler. A gate whose tolerance was retuned to fit a new sampler
is a weaker gate than one left alone, and in a diff it looks identical to a legitimate retune. If
RQMC cannot meet the pseudo-random SE ceiling, RQMC has failed to deliver and the gate should go
red. The path budget (R, n_paths) is the free variable; the tolerance literal is not.

Path budget: R=64 replicates x n_paths=1024 per replicate (1024 is a power of two, required by
`SobolRandomSource`) = 65,536 total paths, against the pseudo gates' 20,000. RQMC therefore starts
with MORE raw paths but a far smaller EFFECTIVE sample (R=64 independent scramble replicates, not
65,536 independent draws) -- that is the honest comparison this file makes, not "RQMC wins because
it touches more paths." R=64 specifically because `rqmc_estimate`'s own SE-calibration table
(models/estimator.py docstring) puts the reported SE at ~0.85 of the true between-replicate spread
at R=64; below that the error bar is materially optimistic (~0.62 of true spread at R=16).

Gates deliberately NOT re-run here, with reasons (unchanged from slice 1/2, sampler-irrelevant),
classified by the PROPERTY each one asserts rather than by any variance worry -- scrambled RQMC is
unbiased, so a poor variance ratio cannot redden any 3-SE bias conjunct on its own:

- G3a (certain call at first observation, se == 0 exactly) -- asserts an EXACT PER-PATH IDENTITY.
  Under RQMC every replicate mean is equal too, so it would pass trivially. Zero information.
- G5 (KO + KI == vanilla per path) -- asserts an EXACT PER-PATH IDENTITY, sampler-irrelevant.
- G4 companion (CONTINUOUS_BRIDGE vs DISCRETE paired difference) -- asserts a MONITORING
  APPROXIMATION agreement, not a sampler property.
- X (QE vs Euler on a fine grid, n_steps=500) -- asserts SCHEME AGREEMENT, which is
  sampler-independent, and it is the most expensive gate in the suite. The slice-3 study covers
  both schemes instead.

G3b (autocallable short-put decomposition) is NOT in this exclusion list: it is gated here as
G3b-Q, below. `products/autocallable.py`'s ledger is `(n_paths, n_obs)` -- `discount()` applies a
different factor per column -- while `products/barrier.py`'s is `(n_paths, 1)`, one column at
expiry; G4-Q above only ever exercises that single-column path. G3b-Q is therefore what covers the
multi-column RQMC ledger, not G4-Q.

FINDING, and why G1-Q parametrizes n_steps alongside scheme instead of mirroring G1's n_steps=50
for both arms: full-truncation Euler at n_steps=50 in the Feller-violating regime
(_FELLER_VIOLATING, shared with G1) carries a REAL discretization bias of ~+0.058 against
`heston_vanilla_price` -- confirmed with a 200k-path PSEUDO-random control run (seed=999,
antithetic=True): pv=7.330222, ref=7.272096, se=0.015782, z=+3.68. This is NOT an RQMC or bridge
artifact (the control run used plain `PseudoRandomSource`, no Sobol, no bridge). QE shows no such
bias at the same n_steps=50 (same control: pv=7.279894, se=0.015079, z=+0.52).

G1's own pseudo gate (`test_validation_gates.py`, unmodified by this slice) never surfaces this:
at n_paths=20_000 its se is ~0.049, so the euler-ft bias sits at only ~1.2 SE of separation --
under the 3-SE threshold purely for want of statistical power, not because the scheme agrees with
the closed form at n_steps=50. This is a property of that file's existing G1, not a defect this
slice introduces; whether G1 itself should be tightened (more paths, fewer schemes, or a
scheme-specific step count) is a separate decision for the orchestrator/architect, out of scope
here.

RQMC's between-replicate SE at R=64 (~0.012-0.017) has enough power to resolve this bias as a
clean, reproducible 3-SE failure at n_steps=50 for every R in {64, 128} and n_paths in {1024,
2048} tried -- raising the budget makes it FAIL HARDER (SE shrinks, the bias does not), the
signature of real bias rather than sampler noise. RQMC cannot fix a discretization bias (it
reduces variance, not bias), so the fix is the discretization ADR-006 Amendment 4 SS4 already
specifies: that amendment's own convergence study at this Feller ratio (0.333) records
"QE first satisfies this at n_steps=12 ... full-truncation Euler first satisfies at n_steps=104."
G1-Q therefore parametrizes `(scheme, n_steps)` together -- `("qe", 50)` and `("euler-ft", 104)`
-- rather than reusing G1's single n_steps=50 for both arms; G1's own use of 50 for both schemes
predates RQMC's tighter SE making the gap load-bearing. At n_steps=104 euler-ft passes cleanly
(seed=42: pv=7.298750, se=0.016782, ref=7.272096, z=+1.59; 20/20 across seeds 1-20).
"""

from __future__ import annotations

import math

import pytest

from exo.models.analytic import bs_barrier_price, bs_call_price
from exo.models.heston_cf import heston_vanilla_price
from exo.models.params import EngineConfig, HestonParams, Scheme
from exo.products.autocallable import Autocallable
from exo.products.barrier import BarrierOption
from exo.products.base import Monitoring
from exo.products.pricer import price_rqmc

# Same params as G1/X, shared with test_validation_gates.py / test_heston_paths.py.
_FELLER_VIOLATING = HestonParams(
    s0=100.0, r=0.02, q=0.01, v0=0.04, kappa=1.5, theta=0.04, xi=0.6, rho=-0.7
)
_STRIKE = 100.0
_EXPIRY = 1.0
_N_STEPS = 50
_R = 64
_N_PATHS = 1024


def _vanilla_call_stand_in(strike: float, expiry: float) -> BarrierOption:
    """A knock-out call with a barrier never crossed, so `weight == 1` and the payoff is exactly
    `max(S_T - strike, 0)` -- same trick `test_pricer_rqmc.py` uses, avoiding a second payoff type
    just to get a plain vanilla call through `price_rqmc` (which prices a `Payoff`, not a bare
    array)."""
    return BarrierOption(
        underlying="TEST",
        option_type="call",
        strike=strike,
        expiry=expiry,
        barrier=1.0e6,
        direction="up",
        knock="out",
        monitoring=Monitoring.CONTINUOUS_BRIDGE,
    )


@pytest.mark.parametrize(
    ("scheme", "n_steps", "seed"),
    [
        ("qe", 50, 42),
        # euler-ft runs at n_steps=104, NOT G1's n_steps=50, per ADR-006 Amendment 4 SS4's own
        # convergence study at this Feller ratio (0.333): "QE first satisfies this at n_steps=12
        # ... full-truncation Euler first satisfies at n_steps=104." G1's use of 50 for both
        # schemes predates that step count being load-bearing -- at 20k pseudo-random paths the
        # gap was buried under sampling noise (see this module's docstring, "FINDING"); RQMC's
        # tighter SE resolves it, so this arm uses the ADR's own documented convergence point
        # rather than mirroring G1's n_steps=50. Do not "simplify" the two arms back to one
        # n_steps without re-reading that FINDING paragraph.
        ("euler-ft", 104, 42),
    ],
)
def test_g1_q_rqmc_matches_heston_cf_within_3se(scheme: Scheme, n_steps: int, seed: int) -> None:
    """G1-Q. RQMC counterpart of G1 (test_validation_gates.py). No control variate, deliberately:
    the natural control here is the Heston vanilla itself, whose analytic mean *is* G1's own
    reference -- a CV'd G1 would be a tautology testing nothing (and collapses `apply_control`'s
    output to a zero-variance constant exactly when the control and the product coincide).

    Measured (R=64, n_paths=1024, bridge={"spot"}, seed=42):
        qe,       n_steps=50:  pv=7.278570  se=0.014368  ref=7.272096  z=+0.451
        euler-ft, n_steps=104: pv=7.298750  se=0.016782  ref=7.272096  z=+1.588
    """
    engine = EngineConfig(
        scheme=scheme, n_steps=n_steps, n_paths=_N_PATHS, expiry=_EXPIRY, antithetic=False
    )
    payoff = _vanilla_call_stand_in(_STRIKE, _EXPIRY)
    result = price_rqmc(
        payoff,
        _FELLER_VIOLATING,
        engine,
        seed=seed,
        n_replicates=_R,
        bridge=frozenset({"spot"}),
    )
    reference = heston_vanilla_price(_FELLER_VIOLATING, _STRIKE, _EXPIRY, is_call=True)

    tol_abs = 0.06  # same tol_abs as G1 in test_validation_gates.py; copied, never re-measured
    # for the sampler.
    assert 0.0 < result.std_err < tol_abs, (
        f"{scheme}: se={result.std_err} is not tight enough to be a meaningful gate "
        f"(tol_abs={tol_abs})"
    )
    assert abs(result.pv - reference) < 3.0 * result.std_err, (
        f"{scheme}: pv={result.pv}, ref={reference}, "
        f"off by {(result.pv - reference) / result.std_err:.2f} SE"
    )


@pytest.mark.parametrize("scheme", ["qe", "euler-ft"])
def test_g2_q_rqmc_matches_black_scholes_in_degenerate_limit_within_3se(scheme: Scheme) -> None:
    """G2-Q. RQMC counterpart of G2 (test_validation_gates.py): xi -> ~0, v0 = theta = sigma**2,
    rho = 0 collapses Heston to Black-Scholes. No control variate (same reason as G1-Q).

    Measured (R=64, n_paths=1024, n_steps=50, bridge={"spot"}, seed=123):
        qe:       pv=8.350145  se=0.001489  ref=8.349406  z=+0.496
        euler-ft: pv=8.350145  se=0.001489  ref=8.349406  z=+0.496
    (identical between schemes: with xi=1e-4 the variance process is essentially deterministic,
    so the scheme choice barely perturbs the path -- no discrepancy analogous to G1-Q's.)
    """
    sigma = 0.2
    degenerate = HestonParams(
        s0=100.0, r=0.02, q=0.01, v0=sigma**2, kappa=1.5, theta=sigma**2, xi=1e-4, rho=0.0
    )
    engine = EngineConfig(
        scheme=scheme, n_steps=_N_STEPS, n_paths=_N_PATHS, expiry=_EXPIRY, antithetic=False
    )
    payoff = _vanilla_call_stand_in(_STRIKE, _EXPIRY)
    result = price_rqmc(
        payoff, degenerate, engine, seed=123, n_replicates=_R, bridge=frozenset({"spot"})
    )
    reference = bs_call_price(
        s0=100.0, strike=_STRIKE, r=degenerate.r, q=degenerate.q, sigma=sigma, expiry=_EXPIRY
    )

    tol_abs = 0.09  # same tol_abs as G2 in test_validation_gates.py; copied, never re-measured
    # for the sampler.
    assert 0.0 < result.std_err < tol_abs, (
        f"{scheme}: se={result.std_err} is not tight enough to be a meaningful gate "
        f"(tol_abs={tol_abs})"
    )
    assert abs(result.pv - reference) < 3.0 * result.std_err, (
        f"{scheme}: pv={result.pv}, bs_ref={reference}, "
        f"off by {(result.pv - reference) / result.std_err:.2f} SE"
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


def _g4_barrier_option() -> BarrierOption:
    return BarrierOption(
        underlying="SYN",
        option_type="call",
        strike=_G4_STRIKE,
        expiry=_EXPIRY,
        barrier=_G4_BARRIER,
        direction="down",
        knock="out",
        monitoring=Monitoring.CONTINUOUS_BRIDGE,
    )


def test_g4_q_down_and_out_call_matches_bs_barrier_within_3se() -> None:
    """G4-Q. RQMC counterpart of G4 (test_validation_gates_products.py): down-and-out call,
    model degenerated to Black-Scholes, CONTINUOUS_BRIDGE monitoring, vs `bs_barrier_price`. No
    control variate (that's G4-Q-CV, below).

    Measured (R=64, n_paths=1024, n_steps=50, bridge={"spot"}, seed=42):
        pv=6.795259  se=0.011419  ref=6.807708  z=-1.090
    """
    engine = EngineConfig(
        scheme="qe", n_steps=_N_STEPS, n_paths=_N_PATHS, expiry=_EXPIRY, antithetic=False
    )
    result = price_rqmc(
        _g4_barrier_option(),
        _DEGENERATE,
        engine,
        seed=42,
        n_replicates=_R,
        bridge=frozenset({"spot"}),
    )
    reference = bs_barrier_price(
        s0=_DEGENERATE.s0,
        strike=_G4_STRIKE,
        r=_DEGENERATE.r,
        q=_DEGENERATE.q,
        sigma=_SIGMA_DEGENERATE,
        expiry=_EXPIRY,
        barrier=_G4_BARRIER,
        direction="down",
        knock="out",
        option_type="call",
    )

    tol_abs = 0.09  # same tol_abs as G4 in test_validation_gates_products.py; copied, never
    # re-measured for the sampler.
    assert 0.0 < result.std_err < tol_abs, (
        f"se={result.std_err} is not tight enough to be a meaningful gate (tol_abs={tol_abs})"
    )
    assert abs(result.pv - reference) < 3.0 * result.std_err, (
        f"pv={result.pv}, bs_ref={reference}, off by "
        f"{(result.pv - reference) / result.std_err:.2f} SE"
    )


def test_g4_q_cv_down_and_out_call_matches_bs_barrier_within_3se() -> None:
    """G4-Q-CV. Same term sheet as G4-Q, WITH the Heston-vanilla control variate
    (`control_strike=_G4_STRIKE`, `beta=None` so `price_rqmc`'s pilot replicate fits it) -- this
    is the barrier product, so (unlike G1-Q/G2-Q's plain vanilla stand-in) the control's analytic
    mean is not the reference itself, and the CV is not a tautology here.

    Measured (R=64, n_paths=1024, n_steps=50, bridge={"spot"}, seed=42):
        pv=6.795945  se=0.011657  ref=6.807708  z=-1.009
    (se is not materially smaller than G4-Q's here -- the barrier is far from ATM (90 vs strike
    100), so payoff/control correlation is weak at this configuration; recorded honestly rather
    than picked for a flattering ratio.)
    """
    engine = EngineConfig(
        scheme="qe", n_steps=_N_STEPS, n_paths=_N_PATHS, expiry=_EXPIRY, antithetic=False
    )
    result = price_rqmc(
        _g4_barrier_option(),
        _DEGENERATE,
        engine,
        seed=42,
        n_replicates=_R,
        bridge=frozenset({"spot"}),
        control_strike=_G4_STRIKE,
    )
    reference = bs_barrier_price(
        s0=_DEGENERATE.s0,
        strike=_G4_STRIKE,
        r=_DEGENERATE.r,
        q=_DEGENERATE.q,
        sigma=_SIGMA_DEGENERATE,
        expiry=_EXPIRY,
        barrier=_G4_BARRIER,
        direction="down",
        knock="out",
        option_type="call",
    )

    tol_abs = 0.09  # same tol_abs as G4 in test_validation_gates_products.py; copied, never
    # re-measured for the sampler.
    assert 0.0 < result.std_err < tol_abs, (
        f"se={result.std_err} is not tight enough to be a meaningful gate (tol_abs={tol_abs})"
    )
    assert abs(result.pv - reference) < 3.0 * result.std_err, (
        f"pv={result.pv}, bs_ref={reference}, off by "
        f"{(result.pv - reference) / result.std_err:.2f} SE"
    )


_G3B_AAPL = HestonParams(
    s0=187.50, r=0.0425, q=0.0050, v0=0.0400, kappa=1.50, theta=0.0400, xi=0.60, rho=-0.70
)


def test_g3b_q_autocallable_degenerate_short_put_within_3se() -> None:
    """G3b-Q. RQMC counterpart of G3b (test_validation_gates_products.py): same degenerate
    fixture (autocall_trigger/coupon_barrier unreachably high at 1e7, protection_barrier ==
    initial_level == s0), which collapses the terminal leg exactly to a short put struck at s0
    -- the degenerate fixture is the point here, not a shortcoming to fix, since it is what
    makes the identity closed-form-exact. This is what exercises the multi-column
    `(n_paths, n_obs)` ledger under RQMC (`autocallable.py`'s `discount()` applies a different
    factor per column); G4-Q above only ever exercises `barrier.py`'s single-column
    `(n_paths, 1)` ledger. No control variate (mirrors G1-Q/G2-Q).

    Measured (R=64, n_paths=1024, n_steps=50, bridge={"spot"}, seed=202):
        pv=904.763122  se=0.156489  ref=904.864877  z=-0.650
    """
    notional = 1000.0
    s0 = _G3B_AAPL.s0
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
    engine = EngineConfig(
        scheme="qe", n_steps=_N_STEPS, n_paths=_N_PATHS, expiry=_EXPIRY, antithetic=False
    )
    result = price_rqmc(
        note, _G3B_AAPL, engine, seed=202, n_replicates=_R, bridge=frozenset({"spot"})
    )
    reference = notional * math.exp(-_G3B_AAPL.r * 1.0) - (notional / s0) * heston_vanilla_price(
        _G3B_AAPL, strike=s0, expiry=1.0, is_call=False
    )

    tol_abs = 0.8  # same tol_abs as G3b in test_validation_gates_products.py; copied, never
    # re-measured for the sampler.
    assert 0.0 < result.std_err < tol_abs, (
        f"se={result.std_err} is not tight enough to be a meaningful gate (tol_abs={tol_abs})"
    )
    assert abs(result.pv - reference) < 3.0 * result.std_err, (
        f"pv={result.pv}, ref={reference}, off by {(result.pv - reference) / result.std_err:.2f} SE"
    )
