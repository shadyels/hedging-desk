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

        Uses inverse-variance weighting: the minimum-variance unbiased combination
        of two independent unbiased estimates of the same quantity, weighting each
        by `1/std_err**2`. This needs no assumption about how many independent
        draws underlie each `std_err` (which the antithetic-vs-plain distinction
        already folds into the number), and it exists so a convergence study can
        reach millions of paths while peak memory stays at ONE batch: run N
        independent batches through `simulate()` + `mc_estimate()` and fold them
        together with `combine()`.

        When both operands have equal std_err (the common case of equal-size
        batches from the same engine config), this reduces exactly to the plain
        average of the two means and `std_err / sqrt(2)`.
        """
        w_self = 1.0 / self.std_err**2
        w_other = 1.0 / other.std_err**2
        pv = (w_self * self.pv + w_other * other.pv) / (w_self + w_other)
        std_err = math.sqrt(1.0 / (w_self + w_other))
        return PriceResult(pv=pv, std_err=std_err, n_paths=self.n_paths + other.n_paths)


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
        pair_means = 0.5 * (discounted_payoff[:n_pairs] + discounted_payoff[n_pairs:])
        pv = float(pair_means.mean())
        std_err = float(pair_means.std(ddof=1) / math.sqrt(n_pairs))
    else:
        pv = float(discounted_payoff.mean())
        std_err = float(discounted_payoff.std(ddof=1) / math.sqrt(n_total))

    return PriceResult(pv=pv, std_err=std_err, n_paths=n_total)
