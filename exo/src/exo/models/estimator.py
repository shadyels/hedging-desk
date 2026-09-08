"""Monte Carlo price/standard-error estimation from a PathBundle + payoff.

`mc_estimate` takes the BUNDLE, not a bare payoff array, so the antithetic pairing
structure cannot be lost or mismatched at the call site.

LOAD-BEARING: under antithetics, the standard error is computed over PAIR MEANS,
not over all `2*n_pairs` individual draws.

    pm = 0.5 * (payoff[:n_pairs] + payoff[n_pairs:])
    pv = pm.mean()
    se = pm.std(ddof=1) / sqrt(n_pairs)

Antithetic draws are NEGATIVELY CORRELATED by construction (that is the entire
point of antithetic variance reduction), so treating all `2*n_pairs` draws as if
they were independent samples of one distribution overstates the standard error
by exactly the factor antithetics reduced it by. Measured in a planning spike (ATM
call, 200k pairs): naive (over all draws) SE = 0.021084, pair-mean SE = 0.016457 —
an overstatement of 1.28x. Plain MC over the same number of draws gives SE =
0.021128, so the antithetic variance reduction is ALSO 1.28x: these are the same
number. Using the naive estimator throws away exactly the benefit antithetics
bought, while simultaneously making every "within 3 standard errors" acceptance
gate correspondingly easier to pass — silently weakening the one check this
package relies on for correctness.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from exo.models.heston import PathBundle


@dataclass(frozen=True)
class PriceResult:
    """A Monte Carlo price estimate and its standard error, from `n_paths` paths."""

    pv: float
    std_err: float
    n_paths: int

    def combine(self, other: PriceResult) -> PriceResult:
        """Pool two independent PriceResults — NOT a re-simulation.

        Weights by RAW PATH COUNT (`n_paths`), i.e. exactly what pooling the underlying draws
        into one larger sample would give. It exists so a convergence study can reach millions
        of paths while peak memory stays at ONE batch: run N independent batches through
        `simulate()` + `mc_estimate()` and fold them together with `combine()`.

        This deliberately replaced an earlier inverse-variance-weighted version (weighting each
        operand by `1/std_err**2`): that scheme's weights are ESTIMATED FROM THE SAME DATA they
        weight, which introduces a small bias of order
        `-(1 - 1/B)*Cov(X_bar, S**2)/sigma**2` in the pooled mean (B = batch count) -- zero in
        the two-operand degenerate case only when std_err happens to be equal, and otherwise
        growing with the number of times `combine()` is left-folded. That growth tracks exactly
        the axis a convergence study's step-count sweep varies (more steps -> smaller
        per-batch paths under a fixed memory ceiling -> more batches), which can mimic the
        signature of genuine discretization bias at precisely the fine step counts where true
        bias should be near zero. n_paths-weighting has no such term: it needs no variance
        estimate at all, so there is nothing for the same data to bias.

        n_paths-weighting is also well-defined when one operand has `std_err == 0.0` (inverse-
        variance weighting divides by it and raises `ZeroDivisionError`), and, when both operands
        have EQUAL std_err (the common case of equal-size batches from the same engine config),
        it reduces exactly to the plain average of the two means and `std_err / sqrt(2)` -- the
        one case where the two weighting schemes coincide.
        """
        n = self.n_paths + other.n_paths
        w_self = self.n_paths / n
        w_other = other.n_paths / n
        pv = w_self * self.pv + w_other * other.pv
        std_err = math.sqrt((w_self * self.std_err) ** 2 + (w_other * other.std_err) ** 2)
        return PriceResult(pv=pv, std_err=std_err, n_paths=n)


def mc_estimate(bundle: PathBundle, discounted_payoff: NDArray[np.float64]) -> PriceResult:
    """Estimate a price and its standard error from a discounted payoff array.

    `discounted_payoff` must have the same leading dimension as `bundle.S`
    (one value per simulated path). Under antithetics, the estimate collapses to
    pair means first (see module docstring); otherwise it is the plain sample
    mean/SE.
    """
    n_total = discounted_payoff.shape[0]
    if n_total != bundle.S.shape[0]:
        raise ValueError(
            f"discounted_payoff has {n_total} paths but bundle has {bundle.S.shape[0]}"
        )

    if bundle.antithetic:
        if bundle.n_pairs is None:
            raise ValueError("antithetic PathBundle must carry n_pairs")
        n_pairs = bundle.n_pairs
        # ddof=1 needs >= 2 samples; n_pairs=1 is reachable (resolve_batch_plan floors a batch
        # to a minimum of 2 paths = 1 antithetic pair) and would otherwise return a silent NaN
        # standard error (P1-6, code review 2026-09-06) rather than failing loudly.
        if n_pairs < 2:
            raise ValueError(
                f"mc_estimate needs at least 2 antithetic pairs to compute a sample standard "
                f"error (ddof=1), got n_pairs={n_pairs}"
            )
        pair_means = 0.5 * (discounted_payoff[:n_pairs] + discounted_payoff[n_pairs:])
        pv = float(pair_means.mean())
        std_err = float(pair_means.std(ddof=1) / math.sqrt(n_pairs))
    else:
        if n_total < 2:
            raise ValueError(
                f"mc_estimate needs at least 2 paths to compute a sample standard error "
                f"(ddof=1), got n_paths={n_total}"
            )
        pv = float(discounted_payoff.mean())
        std_err = float(discounted_payoff.std(ddof=1) / math.sqrt(n_total))

    return PriceResult(pv=pv, std_err=std_err, n_paths=n_total)
