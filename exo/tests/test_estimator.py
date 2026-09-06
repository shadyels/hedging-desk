"""Tests for exo.models.estimator.

The load-bearing property under test: under antithetics, the standard error MUST be
computed over PAIR MEANS, not over all 2*n_pairs draws. The naive estimator
overstates the error because antithetic draws are negatively correlated by
construction — computing sample SE over all of them throws away exactly the
variance reduction antithetics bought. See estimator.py's module docstring for the
measured numbers from the spec's planning spike (naive 0.021084 vs pair-mean
0.016457, a 1.28x overstatement).
"""

import math

import numpy as np
import pytest

from exo.models.estimator import PriceResult, mc_estimate
from exo.models.heston import PathBundle, simulate
from exo.models.params import EngineConfig, HestonParams
from exo.models.rng import PseudoRandomSource


def _antithetic_bundle(n_paths: int, n_steps: int = 10, seed: int = 1) -> PathBundle:
    params = HestonParams(
        s0=100.0, r=0.02, q=0.01, v0=0.04, kappa=1.5, theta=0.04, xi=0.6, rho=-0.7
    )
    engine = EngineConfig(
        scheme="qe", n_steps=n_steps, n_paths=n_paths, expiry=1.0, antithetic=True
    )
    return simulate(params, engine, PseudoRandomSource(seed=seed))


def test_naive_and_pair_mean_estimators_provably_differ_on_antithetic_input() -> None:
    """A hand-built, perfectly-anticorrelated antithetic payoff: pairs (1, 5) and
    (3, 3) each average to exactly 3, so the CORRECT pair-mean SE is exactly zero,
    while the naive over-all-draws SE is nonzero. This is what stops the correct
    estimator being silently swapped for the naive one — the two formulas cannot
    both be "computing the same thing" if one reports zero and the other doesn't."""
    n_pairs = 2
    payoff = np.array([1.0, 3.0, 5.0, 3.0])

    pair_means = 0.5 * (payoff[:n_pairs] + payoff[n_pairs:])
    correct_se = float(pair_means.std(ddof=1) / math.sqrt(n_pairs))
    naive_se = float(payoff.std(ddof=1) / math.sqrt(payoff.shape[0]))

    assert correct_se == pytest.approx(0.0, abs=1e-12)
    assert naive_se > 0.5
    assert correct_se != pytest.approx(naive_se)


def test_mc_estimate_uses_pair_mean_se_not_naive_se() -> None:
    n_pairs = 2
    payoff = np.array([1.0, 3.0, 5.0, 3.0])
    bundle = _antithetic_bundle(n_paths=2 * n_pairs)
    # Only the antithetic pairing metadata matters to mc_estimate, not the actual
    # simulated payoff — substitute the hand-built payoff to pin the exact SE.
    result = mc_estimate(bundle, payoff)
    assert result.pv == pytest.approx(3.0)
    assert result.std_err == pytest.approx(0.0, abs=1e-12)


def test_mc_estimate_pair_mean_se_smaller_than_naive_on_realistic_payoff() -> None:
    """On a real (not hand-crafted) antithetic MC run, the pair-mean SE should be
    smaller than the naive full-sample SE — the direction of the spec's measured
    1.28x overstatement, even though the exact factor is scale/seed dependent."""
    n_pairs = 20_000
    bundle = _antithetic_bundle(n_paths=2 * n_pairs, n_steps=20, seed=99)
    discounted_call = np.maximum(bundle.S[:, -1] - 100.0, 0.0)

    result = mc_estimate(bundle, discounted_call)
    naive_se = float(discounted_call.std(ddof=1) / math.sqrt(discounted_call.shape[0]))

    assert result.std_err < naive_se


def test_mc_estimate_ddof1_on_non_antithetic_bundle() -> None:
    params = HestonParams(
        s0=100.0, r=0.02, q=0.01, v0=0.04, kappa=1.5, theta=0.04, xi=0.6, rho=-0.7
    )
    engine = EngineConfig(scheme="qe", n_steps=5, n_paths=100, expiry=1.0, antithetic=False)
    bundle = simulate(params, engine, PseudoRandomSource(seed=5, antithetic=False))
    payoff = np.maximum(bundle.S[:, -1] - 100.0, 0.0)

    result = mc_estimate(bundle, payoff)
    expected_se = float(payoff.std(ddof=1) / math.sqrt(payoff.shape[0]))
    assert result.std_err == pytest.approx(expected_se)
    assert result.pv == pytest.approx(float(payoff.mean()))
    assert result.n_paths == payoff.shape[0]


def test_combine_pooled_mean_close_to_single_batch_reference() -> None:
    n_pairs_each = 5000
    bundle_a = _antithetic_bundle(n_paths=2 * n_pairs_each, n_steps=15, seed=11)
    bundle_b = _antithetic_bundle(n_paths=2 * n_pairs_each, n_steps=15, seed=12)

    payoff_a = np.maximum(bundle_a.S[:, -1] - 100.0, 0.0)
    payoff_b = np.maximum(bundle_b.S[:, -1] - 100.0, 0.0)

    result_a = mc_estimate(bundle_a, payoff_a)
    result_b = mc_estimate(bundle_b, payoff_b)
    combined = result_a.combine(result_b)

    # Single "reference" batch of the same total path count, same seed family.
    reference_bundle = _antithetic_bundle(n_paths=4 * n_pairs_each, n_steps=15, seed=13)
    reference_payoff = np.maximum(reference_bundle.S[:, -1] - 100.0, 0.0)
    reference = mc_estimate(reference_bundle, reference_payoff)

    assert combined.n_paths == result_a.n_paths + result_b.n_paths
    # Both are independent MC estimates of the same true price: they must agree
    # within a handful of combined standard errors, not bitwise.
    combined_se = math.sqrt(combined.std_err**2 + reference.std_err**2)
    assert abs(combined.pv - reference.pv) < 5 * combined_se
    # combine() must actually reduce the SE relative to either half.
    assert combined.std_err < result_a.std_err
    assert combined.std_err < result_b.std_err


def test_combine_is_exact_for_equal_variance_batches() -> None:
    """When two batches have IDENTICAL std_err (the common case: equal-size batches
    of the same engine config), n_paths-weighted combine() must reduce to
    a plain average of the means and se/sqrt(2) — an exactly checkable case."""
    a = PriceResult(pv=10.0, std_err=0.5, n_paths=1000)
    b = PriceResult(pv=12.0, std_err=0.5, n_paths=1000)
    combined = a.combine(b)
    assert combined.pv == pytest.approx(11.0)
    assert combined.std_err == pytest.approx(0.5 / math.sqrt(2))
    assert combined.n_paths == 2000


def test_combine_uses_n_paths_weighting_not_inverse_variance_weighting() -> None:
    """P0-4 (code review, 2026-09-06): combine() pools by RAW PATH COUNT (as if pooling the
    underlying draws), not by inverse-variance. Inverse-variance weighting uses variances
    ESTIMATED FROM THE SAME DATA it weights, which the reviewer showed introduces a bias of
    `-(1 - 1/B)*Cov(X_bar, S^2)/sigma^2` -- zero at few batches, growing with batch count B --
    exactly the axis docs/studies/p2m1-scheme-convergence.md's convergence table sweeps.

    Unequal std_err operands are REQUIRED to tell the two formulas apart: they coincide when
    std_err is equal (test_combine_is_exact_for_equal_variance_batches, unaffected by this
    change), so a regression back to inverse-variance weighting would still pass that test.
    """
    a = PriceResult(pv=10.0, std_err=1.0, n_paths=100)
    b = PriceResult(pv=20.0, std_err=2.0, n_paths=300)
    combined = a.combine(b)

    # n_paths-weighted: w_a = 100/400 = 0.25, w_b = 300/400 = 0.75
    assert combined.pv == pytest.approx(0.25 * 10.0 + 0.75 * 20.0)  # == 17.5
    assert combined.std_err == pytest.approx(math.sqrt((0.25 * 1.0) ** 2 + (0.75 * 2.0) ** 2))
    assert combined.n_paths == 400

    # Inverse-variance weighting (the OLD, now-removed behavior) would give pv=12.0 -- pin that
    # we are NOT that, so this regresses loudly if combine() reverts.
    assert combined.pv != pytest.approx(12.0)


def test_combine_does_not_divide_by_zero_when_one_operand_has_zero_std_err() -> None:
    """P0-4: inverse-variance weighting divides by std_err**2 and raises ZeroDivisionError on a
    zero-SE operand (P1-6 makes a degenerate single-sample SE a loud ValueError instead of a
    silent NaN, but a LEGITIMATELY zero SE -- e.g. a hand-built PriceResult in a test, or a
    perfectly-canceling antithetic payoff -- must still combine cleanly)."""
    a = PriceResult(pv=10.0, std_err=0.0, n_paths=100)
    b = PriceResult(pv=12.0, std_err=0.5, n_paths=100)
    combined = a.combine(b)  # must not raise ZeroDivisionError
    assert combined.pv == pytest.approx(11.0)
    assert combined.std_err == pytest.approx(0.25)


def test_mc_estimate_raises_on_single_antithetic_pair() -> None:
    """P1-6: n_pairs=1 makes pair_means.std(ddof=1) a silent NaN (need >=2 samples for ddof=1).
    resolve_batch_plan floors a batch to a minimum of 2 paths (1 antithetic pair), so this is
    reachable, not merely theoretical -- mc_estimate must fail loudly instead."""
    bundle = _antithetic_bundle(n_paths=2, n_steps=3, seed=1)
    payoff = np.array([1.0, 2.0])
    with pytest.raises(ValueError, match="at least 2"):
        mc_estimate(bundle, payoff)


def test_mc_estimate_raises_on_single_path_non_antithetic() -> None:
    """P1-6: the non-antithetic branch has the same ddof=1 degenerate case at n_paths=1."""
    params = HestonParams(
        s0=100.0, r=0.02, q=0.01, v0=0.04, kappa=1.5, theta=0.04, xi=0.6, rho=-0.7
    )
    engine = EngineConfig(scheme="qe", n_steps=3, n_paths=1, expiry=1.0, antithetic=False)
    bundle = simulate(params, engine, PseudoRandomSource(seed=5, antithetic=False))
    payoff = np.array([1.0])
    with pytest.raises(ValueError, match="at least 2"):
        mc_estimate(bundle, payoff)
