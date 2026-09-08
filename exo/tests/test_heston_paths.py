"""Tests for exo.models.heston: PathBundle + simulate().

Covers shapes, the QE non-negativity-by-construction invariant, full-truncation
Euler's signed-variance-state invariant, determinism, and the martingale property
`E[S_T]/S0 == exp((r-q)*T)` within 3 standard errors for BOTH schemes.

The QE martingale test uses a DELIBERATELY COARSE step count (n_steps=4 over T=1,
per the orchestrator's 2026-09-06 amendment A1) because a wrong or missing
martingale correction on K0 produces a large, easily-detected bias at coarse dt —
this is what actually exercises the branch-dependent K0* correction rather than
being swamped by small-dt behavior converging to "correct" regardless.
"""

import math

import numpy as np
import pytest
from pydantic import ValidationError

from exo.models.estimator import mc_estimate
from exo.models.heston import simulate
from exo.models.params import EngineConfig, HestonParams
from exo.models.rng import PseudoRandomSource

# Illustrative Heston parameters, deliberately Feller-VIOLATING
# (2*kappa*theta/xi**2 = 2*1.5*0.04/0.36 = 0.333 < 1): this is the regime where QE
# and full-truncation Euler actually differ, per exo/CLAUDE.md engine constraints.
FELLER_VIOLATING = HestonParams(
    s0=100.0, r=0.02, q=0.01, v0=0.04, kappa=1.5, theta=0.04, xi=0.6, rho=-0.7
)


def _engine(scheme: str, n_steps: int, n_paths: int, expiry: float = 1.0) -> EngineConfig:
    return EngineConfig(
        scheme=scheme, n_steps=n_steps, n_paths=n_paths, expiry=expiry, antithetic=True
    )


@pytest.mark.parametrize("scheme", ["qe", "euler-ft"])
def test_bundle_shapes(scheme: str) -> None:
    engine = _engine(scheme, n_steps=20, n_paths=100)
    rng = PseudoRandomSource(seed=1)
    bundle = simulate(FELLER_VIOLATING, engine, rng)
    assert bundle.t.shape == (21,)
    assert bundle.S.shape == (100, 21)
    assert bundle.v.shape == (100, 21)
    assert bundle.antithetic is True
    assert bundle.n_pairs == 50


def test_qe_variance_is_exactly_nonnegative() -> None:
    engine = _engine("qe", n_steps=50, n_paths=2000)
    rng = PseudoRandomSource(seed=7)
    bundle = simulate(FELLER_VIOLATING, engine, rng)
    assert np.all(bundle.v >= 0.0)


def test_euler_ft_stores_signed_variance_state() -> None:
    """Full truncation clips the USE of v (in drift/diffusion), never the STATE.
    Under a Feller-violating parameter set with a non-trivial dt, some stored v
    must go negative — if this test can never observe a negative entry, the state
    is being clipped, which is a different (higher-bias) scheme than specified."""
    engine = _engine("euler-ft", n_steps=20, n_paths=20_000, expiry=1.0)
    rng = PseudoRandomSource(seed=7)
    bundle = simulate(FELLER_VIOLATING, engine, rng)
    assert np.any(bundle.v < 0.0)


def test_same_seed_gives_bitwise_identical_paths() -> None:
    engine = _engine("qe", n_steps=10, n_paths=200)
    bundle_a = simulate(FELLER_VIOLATING, engine, PseudoRandomSource(seed=555))
    bundle_b = simulate(FELLER_VIOLATING, engine, PseudoRandomSource(seed=555))
    assert np.array_equal(bundle_a.S, bundle_b.S)
    assert np.array_equal(bundle_a.v, bundle_b.v)


# Deliberately more extreme than FELLER_VIOLATING, and chosen to put essentially
# ALL (path, step) cells in QE's QUADRATIC branch (psi <= psi_c) at a single,
# very coarse step (n_steps=1, T=1) — low xi keeps psi small; large v0/kappa*T
# makes the correction's magnitude large. This is what actually exercises the
# quadratic branch's K0* term: a mild parameter set (e.g. feller_ratio=0.33,
# moderate xi) puts most mass in the EXPONENTIAL branch at coarse dt instead, so a
# broken quadratic-branch correction can hide undetected. Measured with an
# injected sign error in the quadratic branch's log term (see this worker's final
# report): off by -802 SE — a screamingly loud failure — versus -1.04 SE for the
# correct sign. That gap is what makes this a meaningful gate; the milder
# FELLER_VIOLATING set at n_steps=4 measured only -0.42 SE (correct) vs -0.48 SE
# (no correction at all) — indistinguishable, hence not used here.
MARTINGALE_STRESS = HestonParams(
    s0=100.0, r=0.02, q=0.01, v0=0.16, kappa=2.0, theta=0.04, xi=0.3, rho=-0.9
)


@pytest.mark.parametrize("scheme", ["qe", "euler-ft"])
def test_martingale_property_coarse_dt(scheme: str) -> None:
    """E[S_T * exp(-(r-q)*T)] / s0 must be 1 within 3 SE, at a COARSE step count
    (n_steps=1, T=1) and stress parameters chosen specifically to make a wrong QE
    martingale correction fail loudly rather than being hidden either by small-dt
    convergence or by a parameter set/branch mix too mild to expose the
    correction's effect.

    MARTINGALE_STRESS's psi (~0.48, see the comment above it) lands QE entirely in the
    QUADRATIC branch at n_steps=1 -- test_martingale_property_coarse_dt_exponential_branch
    below is the exponential-branch counterpart (P1-2, code review 2026-09-06).

    For scheme="euler-ft" specifically, this is a SMOKE CHECK, not a discretization-bias test:
    at n_steps=1, `v_plus = max(v0, 0) = v0` is a deterministic constant (there is no prior state
    to truncate), so the Euler step is exactly `x' = x + (r-q-0.5*v0)*dt + sqrt(v0*dt)*Z` --
    an exactly lognormal terminal distribution whose martingale property holds by construction,
    independent of whether full-truncation Euler is implemented correctly at a REAL (non-trivial)
    step count. The bias-sensitive euler-ft check lives in
    `test_validation_gates.py`'s G1 gate (n_steps=50, vs the Heston characteristic function).
    """
    engine = _engine(scheme, n_steps=1, n_paths=200_000, expiry=1.0)
    rng = PseudoRandomSource(seed=2024)
    bundle = simulate(MARTINGALE_STRESS, engine, rng)

    discount = math.exp(-(MARTINGALE_STRESS.r - MARTINGALE_STRESS.q) * engine.expiry)
    discounted_terminal = discount * bundle.S[:, -1]
    result = mc_estimate(bundle, discounted_terminal)

    ratio = result.pv / MARTINGALE_STRESS.s0
    se_ratio = result.std_err / MARTINGALE_STRESS.s0

    assert se_ratio < 0.01, f"SE too loose to be a meaningful gate: se_ratio={se_ratio}"
    assert abs(ratio - 1.0) < 3 * se_ratio, (
        f"{scheme}: E[S_T]/S0 discounted ratio={ratio}, expected 1.0, "
        f"off by {(ratio - 1.0) / se_ratio:.2f} SE"
    )


# Chosen (P1-2, code review 2026-09-06) to land QE's step ENTIRELY in the EXPONENTIAL branch
# (psi > psi_c) at a single coarse step (n_steps=1, T=1): hand-computed (and verified via a
# throwaway script) m=0.041478, s2=0.018707, psi=10.874 >> psi_c=1.5. Without this,
# test_martingale_property_coarse_dt above only exercises the QUADRATIC branch's K0* term
# (MARTINGALE_STRESS's psi ~0.48 there) -- a sign error in the EXPONENTIAL branch's K0* would
# survive untested.
MARTINGALE_STRESS_EXPONENTIAL_BRANCH = HestonParams(
    s0=100.0, r=0.02, q=0.01, v0=0.01, kappa=0.5, theta=0.09, xi=1.0, rho=-0.9
)


def test_martingale_property_coarse_dt_exponential_branch() -> None:
    """Same property and reasoning as test_martingale_property_coarse_dt, but for QE only, with
    MARTINGALE_STRESS_EXPONENTIAL_BRANCH forcing the EXPONENTIAL branch (psi > psi_c) instead of
    the quadratic one."""
    engine = _engine("qe", n_steps=1, n_paths=200_000, expiry=1.0)
    rng = PseudoRandomSource(seed=2025)
    bundle = simulate(MARTINGALE_STRESS_EXPONENTIAL_BRANCH, engine, rng)

    r, q = MARTINGALE_STRESS_EXPONENTIAL_BRANCH.r, MARTINGALE_STRESS_EXPONENTIAL_BRANCH.q
    discount = math.exp(-(r - q) * engine.expiry)
    discounted_terminal = discount * bundle.S[:, -1]
    result = mc_estimate(bundle, discounted_terminal)

    ratio = result.pv / MARTINGALE_STRESS_EXPONENTIAL_BRANCH.s0
    se_ratio = result.std_err / MARTINGALE_STRESS_EXPONENTIAL_BRANCH.s0

    assert se_ratio < 0.01, f"SE too loose to be a meaningful gate: se_ratio={se_ratio}"
    assert abs(ratio - 1.0) < 3 * se_ratio, (
        f"E[S_T]/S0 discounted ratio={ratio}, expected 1.0, "
        f"off by {(ratio - 1.0) / se_ratio:.2f} SE"
    )


def test_simulate_raises_on_antithetic_mismatch() -> None:
    """P1-5 (code review 2026-09-06): EngineConfig.antithetic and RandomSource.antithetic are
    two unreconciled booleans if simulate() doesn't check them. The dangerous direction is
    EngineConfig(antithetic=False) + a mirroring RandomSource(antithetic=True): draws ARE
    mirrored but bundle.antithetic=False, so mc_estimate silently takes the naive (looser) 2N
    branch instead of the correct pair-mean one -- every gate gets GREENER with nothing failing
    loudly. simulate() must refuse this combination instead."""
    engine = _engine("qe", n_steps=4, n_paths=1000, expiry=1.0)  # antithetic=True
    mismatched_rng = PseudoRandomSource(seed=1, antithetic=False)
    with pytest.raises(ValueError, match="antithetic"):
        simulate(FELLER_VIOLATING, engine, mismatched_rng)


def test_qe_exposes_fallback_diagnostic_and_euler_does_not() -> None:
    engine = _engine("qe", n_steps=4, n_paths=1000)
    bundle = simulate(FELLER_VIOLATING, engine, PseudoRandomSource(seed=3))
    assert bundle.qe_fallback_count is not None
    assert bundle.qe_fallback_count >= 0

    engine_euler = _engine("euler-ft", n_steps=4, n_paths=1000)
    bundle_euler = simulate(FELLER_VIOLATING, engine_euler, PseudoRandomSource(seed=3))
    assert bundle_euler.qe_fallback_count is None


# Deliberately extreme (kappa*rho/xi far outside any illustrative parameter set this slice uses)
# to brute-force EVERY (path, step) cell into the martingale correction's inadmissible region
# (SHOULD-5, third code-review round, 2026-09-06). Verified directly before writing this test:
# with n_steps=1, n_paths=2000, expiry=2.0 (dt=2.0), qe_fallback_count == n_paths == 2000 (100%).
QE_FALLBACK_STRESS = HestonParams(
    s0=100.0, r=0.02, q=0.01, v0=0.04, kappa=20.0, theta=0.04, xi=5.0, rho=0.99
)


def test_qe_fallback_count_non_zero_path_is_exercised() -> None:
    """SHOULD-5: every existing assertion on `qe_fallback_count`/`qe_fallback_fraction` is
    satisfied at zero (`>= 0`, `is not None`), and every QE row in the definitive artifact reads
    0.0000% -- an inverted `fallback_mask` (`~quad_admissible` swapped for `quad_admissible`, or
    vice versa in the exponential branch) or a broken aggregation (e.g. summing the wrong array,
    or never incrementing) would look IDENTICAL to a correct implementation under every test that
    only ever observes zero. This forces the inadmissible branch with QE_FALLBACK_STRESS and
    asserts the count is not just non-zero but essentially the WHOLE grid (fraction ~= 1.0),
    matching the value independently verified before this test was written."""
    engine = EngineConfig(scheme="qe", n_steps=1, n_paths=2000, expiry=2.0, antithetic=True)
    bundle = simulate(QE_FALLBACK_STRESS, engine, PseudoRandomSource(seed=1))
    assert bundle.qe_fallback_count is not None
    assert bundle.qe_fallback_count > 0
    fraction = bundle.qe_fallback_count / (engine.n_paths * engine.n_steps)
    assert fraction == pytest.approx(1.0)


def test_heston_params_rejects_rho_outside_open_unit_interval() -> None:
    with pytest.raises(ValidationError):
        HestonParams(s0=100, r=0.0, q=0.0, v0=0.04, kappa=1.0, theta=0.04, xi=0.5, rho=1.5)


def test_heston_params_feller_ratio() -> None:
    assert FELLER_VIOLATING.feller_ratio == pytest.approx(2 * 1.5 * 0.04 / 0.36)


def test_engine_config_rejects_unknown_scheme() -> None:
    with pytest.raises(ValidationError):
        EngineConfig(scheme="tree", n_steps=10, n_paths=100, expiry=1.0, antithetic=True)


def test_engine_config_rejects_odd_n_paths_when_antithetic() -> None:
    with pytest.raises(ValidationError):
        EngineConfig(scheme="qe", n_steps=10, n_paths=101, expiry=1.0, antithetic=True)
