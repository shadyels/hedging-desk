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
    of the same engine config), inverse-variance-weighted combine() must reduce to
    a plain average of the means and se/sqrt(2) — an exactly checkable case."""
    a = PriceResult(pv=10.0, std_err=0.5, n_paths=1000)
    b = PriceResult(pv=12.0, std_err=0.5, n_paths=1000)
    combined = a.combine(b)
    assert combined.pv == pytest.approx(11.0)
    assert combined.std_err == pytest.approx(0.5 / math.sqrt(2))
    assert combined.n_paths == 2000
