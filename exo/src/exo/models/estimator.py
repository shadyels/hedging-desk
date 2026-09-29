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
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from exo.models.heston import PathBundle

_MIN_RQMC_REPLICATES = 8


@dataclass(frozen=True)
class PriceResult:
    """A Monte Carlo price estimate and its standard error, from `n_paths` paths."""

    pv: float
    std_err: float
    n_paths: int
    n_replicates: int | None = None
    """`None` means this result was produced by `mc_estimate` from independent draws.
    An int means it was produced by `rqmc_estimate` from that many scramble
    replicates (Slice 3 T4) -- see `combine`'s guard below for why the two must
    never be pooled by the same formula."""

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

        Raises `ValueError` if either operand has `n_replicates is not None` (Slice 3 T1):
        this pools by RAW PATH COUNT, which assumes the pooled units are independent samples
        of the same distribution -- precisely the assumption RQMC violates. Pooling replicate
        results this way would silently reconstruct the optimistic standard error this task
        exists to outlaw. Batching under RQMC is done by accumulating more replicate means and
        calling `rqmc_estimate` once, not by `combine()`.
        """
        if self.n_replicates is not None or other.n_replicates is not None:
            raise ValueError(
                "PriceResult.combine cannot pool RQMC replicate results (n_replicates is not "
                "None): combine() weights by raw path count, which assumes independent samples "
                "of the same distribution -- an assumption RQMC draws violate. Accumulate more "
                "replicate means and call rqmc_estimate once instead."
            )
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

    Raises `ValueError` if `bundle.low_discrepancy` is true (Slice 3 T1): low-discrepancy
    (RQMC) draws are not independent samples, so the plain sample standard error computed
    here is invalid for them -- the structural twin of this module's antithetic pair-mean-vs-
    naive-SE issue, except RQMC fails in the UNSAFE direction (the naive SE is optimistic, not
    conservative), so this is a hard guard rather than a warning. Use `rqmc_estimate` instead.
    """
    if bundle.low_discrepancy:
        raise ValueError(
            "mc_estimate cannot be used on a low-discrepancy (RQMC) PathBundle: the plain "
            "sample standard error assumes independent draws, which RQMC violates -- it would "
            "silently report an optimistic (too-tight) error bar, making every 3-SE gate "
            "falsely easier to pass. Use rqmc_estimate instead."
        )

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


def rqmc_estimate(replicate_pvs: Sequence[float], *, n_paths_per_replicate: int) -> PriceResult:
    """Estimate a price and standard error from `R` independent RQMC scramble replicates.

    Each element of `replicate_pvs` is one scramble's own price (a scalar mean over
    `n_paths_per_replicate` low-discrepancy paths, e.g. one `price_rqmc` replicate).
    `pv` is the mean of the replicate means; `std_err` is the between-replicate sample
    standard error, `std(replicate_pvs, ddof=1) / sqrt(R)` -- the RQMC analogue of
    `mc_estimate`'s pair-mean SE: each *replicate* is treated as one independent draw,
    never the paths within it (`mc_estimate` already refuses a low-discrepancy bundle
    for exactly this reason).

    R >= 8 is enforced below, but that floor is not generous -- it is the loosest
    floor this estimator can defend. Measured on a d=4 SMOOTH ANALYTIC integrand
    (scipy Sobol, n=2**10, 400 trials) -- a LOW-DIMENSIONAL WORST CASE, NOT a general
    property of the estimator (see the contrary measurement below): scramble
    replicates are genuinely independent (mean off-diagonal correlation ~ -0.005),
    but the replicate-mean distribution has excess kurtosis ~= 65 (Gaussian = 0).
    With tails that heavy, the *sample* standard deviation at small R badly
    underestimates the true spread, and the reported SE inherits that bias -- always
    in the OPTIMISTIC (too-tight) direction, i.e. the unsafe one: it makes every
    `0 < se < tol_abs` gate easier to pass, not harder.

        R     reported SE / true spread   (d=4 smooth analytic integrand)
        8     0.482
        16    0.623
        32    0.758
        64    0.849
        128   0.916
        256   0.944

    (A pseudo-random control run through the identical harness reads ~1.038, so the
    harness itself is sound -- this bias is a real property of the RQMC estimator on
    THIS integrand, not a measurement artifact.)

    Contrary measurement, same estimator, this package's actual configuration: at
    d = 2*n_steps ~= 100 (n=1024, R=32, Heston barrier/autocallable payoffs),
    `docs/studies/p2m1-qmc-variance-reduction.md` measures RQMC calibration factors
    of 0.835-1.314 against pseudo-random controls of 0.876-1.331 IN THE SAME RUN --
    indistinguishable. The heavy-tail penalty measured above does not reproduce on
    these payoffs. The d=4 table above is retained because it is still the only
    evidence for `_MIN_RQMC_REPLICATES = 8` and the gates' R=64, not because it
    describes this package's typical regime.

    Guidance: gates use R >= 64; the convergence study uses R >= 128. R -- the
    replicate count -- is the only knob available if the d=4 heavy-tail regime is
    ever hit. Raising `n_paths_per_replicate` (paths *within* a replicate) does NOT:
    it tightens each replicate's own quasi-Monte-Carlo error, but says nothing about
    how well 8, 16, or 32 replicate MEANS approximate their own population spread,
    which is what this bias measures.

    ponytail: the between-replicate SE returned here is reported UNCORRECTED for
    possible small-R bias -- optimistic by a factor that depends on both R and the
    payoff's tail behaviour. Ceiling: calibration measured in two regimes, differing
    between them (0.48-0.94 at d=4 smooth analytic vs 0.84-1.31 at d~=100 Heston
    barrier/autocallable), and unquantified outside both. Upgrade: a bias-corrected
    or bootstrap standard error. Trigger: P2.M4 publishing `pv_std_err_e9`, or any
    new payoff class whose replicate-mean kurtosis has not been measured.

    Raises `ValueError` if `R < 8`: below that floor the reported SE is more than 2x
    optimistic (0.482 vs the ~0.85 "usable" region above), which is not a defensible
    error bar at any tolerance.
    """
    n_replicates = len(replicate_pvs)
    if n_replicates < _MIN_RQMC_REPLICATES:
        raise ValueError(
            f"rqmc_estimate needs at least {_MIN_RQMC_REPLICATES} replicates to report a "
            f"usable standard error (measured: R=8 SE is ~2x optimistic vs. true spread; "
            f"see this function's docstring), got R={n_replicates}"
        )
    values = np.asarray(replicate_pvs, dtype=np.float64)
    pv = float(values.mean())
    std_err = float(values.std(ddof=1) / math.sqrt(n_replicates))
    return PriceResult(
        pv=pv,
        std_err=std_err,
        n_paths=n_paths_per_replicate * n_replicates,
        n_replicates=n_replicates,
    )
