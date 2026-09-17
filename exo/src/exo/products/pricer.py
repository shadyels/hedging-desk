"""Discounting and MC pricing for anything implementing `Payoff` (products/base.py).

`price_from_bundle` exists so several products can share one simulated `PathBundle` -- the
same trick `studies/scheme_convergence.py` already uses for multi-strike pricing under one
bundle -- which is what P2.M4's portfolio revaluation will need, and what this slice's own G4
companion test uses to compare CONTINUOUS_BRIDGE vs DISCRETE monitoring on perfectly-correlated
paths.

# ponytail (P2-4, P2.M1 slice 2): discounting is FLAT at `params.r` (or the caller-supplied `r`
# in `price_from_bundle`) -- a single scalar rate applied to every cashflow date, no curve.
# Ceiling: no term structure, so any product whose cashflow dates span enough of the curve to
# matter is priced at a slightly wrong forward rate at each leg. Trigger: P4.M1's rates
# foundation, which is the first milestone that has a curve to discount against.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from exo.models.estimator import PriceResult, mc_estimate
from exo.models.heston import PathBundle, simulate
from exo.models.params import EngineConfig, HestonParams
from exo.models.rng import RandomSource
from exo.products.base import CashflowLedger, Payoff


def discount(ledger: CashflowLedger, *, r: float) -> NDArray[np.float64]:
    """Discount every leg of `ledger` at flat rate `r`, summed per path -> `(n_paths,)`."""
    result: NDArray[np.float64] = ledger.amounts @ np.exp(-r * ledger.t)
    return result


def price_from_bundle(payoff: Payoff, bundle: PathBundle, *, r: float) -> PriceResult:
    """Price `payoff` against an already-simulated `bundle`, discounting at flat rate `r`.

    Row order is preserved end to end: `payoff.cashflows` returns a ledger indexed by
    `bundle`'s own path order, `discount` reduces it to one `(n_paths,)` array without
    reordering, and that array is handed to `mc_estimate(bundle, ...)` unchanged -- the
    antithetic pair-mean standard error is positional and would silently mispair otherwise.

    Requires `bundle.t[-1] == payoff.expiry` EXACTLY, for the same reason `price()` requires
    `engine.expiry == payoff.expiry` exactly (see `price()`'s docstring) -- `bundle.t[-1]` is
    `np.linspace`'s endpoint, set to the exact `expiry` it was simulated with, not a value that
    could pick up floating-point drift. This is a BELT, not the only fix (HIGH-1, code review,
    fix round 1): `price_from_bundle` is the function several products SHARE one bundle
    through, and both `barrier.py` and `autocallable.py` independently bound their own
    monitoring/terminal-leg indices to the payoff's own expiry rather than the bundle's last
    column, so a caller that calls `payoff.cashflows(bundle)` directly (bypassing this guard
    entirely, as this slice's own G4 companion test does) is still priced correctly. This guard
    exists so the *pricer-mediated* path fails loud instead of silently pricing the wrong
    contract -- measured, before the fix, at a 13.5 SE silent mispricing on a 2-year bundle
    against a 1-year down-and-out call.

    # ponytail (P2-8, architect review, fix round 3): the exact `bundle.t[-1] != payoff.expiry`
    # guard above means this function CANNOT price a shorter-dated payoff against a shared
    # longer bundle -- which is the single use case this docstring's own opening paragraph
    # advertises for P2.M4's portfolio revaluation. The HIGH-1 fix round (fix round 1) made that
    # case SAFE -- `barrier.py`/`autocallable.py` independently bound their own monitoring/
    # terminal-leg indices to the payoff's own expiry, not the bundle's last column -- but this
    # guard keeps it UNREACHABLE through the public pricer regardless: every current
    # shorter-dated caller (this slice's own G4 companion test included) must bypass
    # `price_from_bundle` entirely and call `discount(payoff.cashflows(bundle), r=...)` directly
    # to get the safe behaviour. Ceiling: shared-bundle pricing through this function is
    # restricted to one common expiry across every payoff sharing the bundle. Trigger: P2.M4,
    # whose portfolio revaluation is the actual consumer of the shared-bundle case -- relaxing
    # this guard (e.g. to `bundle.t[-1] >= payoff.expiry`) is a deliberate P2.M4 decision to make
    # then, against real portfolio-loop requirements, not an oversight to silently fix here.
    """
    if bundle.t[-1] != payoff.expiry:
        raise ValueError(
            f"bundle horizon {bundle.t[-1]} != payoff.expiry {payoff.expiry} -- pricing a "
            "payoff against a bundle simulated to a different horizon would silently price "
            "the wrong contract."
        )
    ledger = payoff.cashflows(bundle)
    discounted = discount(ledger, r=r)
    return mc_estimate(bundle, discounted)


def price(
    payoff: Payoff, params: HestonParams, engine: EngineConfig, rng: RandomSource
) -> PriceResult:
    """Simulate a fresh `PathBundle` under `params`/`engine`/`rng` and price `payoff` against
    it, discounting at `params.r`.

    Requires `engine.expiry == payoff.expiry` EXACTLY (not within a tolerance): both are
    user-specified literals at the construction site, not values derived from a shared
    computation that could pick up floating-point drift, so any mismatch is a genuine
    configuration error -- pricing a 1-year engine against a payoff that pays at a different
    date would silently price the wrong contract. Exact equality makes that error loud rather
    than occasionally, confusingly tolerant.
    """
    if engine.expiry != payoff.expiry:
        raise ValueError(
            f"EngineConfig.expiry={engine.expiry} does not match payoff.expiry="
            f"{payoff.expiry} -- these must match exactly, or the payoff would be priced "
            "against the wrong contract horizon."
        )
    bundle = simulate(params, engine, rng)
    return price_from_bundle(payoff, bundle, r=params.r)
