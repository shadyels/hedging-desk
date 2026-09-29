"""Discounting and MC pricing for anything implementing `Payoff` (products/base.py).

`price_from_bundle` exists so several products can share one simulated `PathBundle` -- the
same trick `studies/scheme_convergence.py` already uses for multi-strike pricing under one
bundle -- which is what P2.M4's portfolio revaluation will need, and what this slice's own G4
companion test uses to compare CONTINUOUS_BRIDGE vs DISCRETE monitoring on perfectly-correlated
paths.

# ponytail (P2-4, P2.M1 slice 2): discounting is flat at one scalar rate, no term structure.
# Ceiling: any product whose cashflow dates span enough of the curve is priced at a slightly
# wrong forward rate. Trigger: P4.M1 rates foundation. See docs/PONYTAIL-DEBT.md.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from exo.models.control_variate import ControlVariate, apply_control, estimate_beta, vanilla_control
from exo.models.estimator import _MIN_RQMC_REPLICATES, PriceResult, mc_estimate, rqmc_estimate
from exo.models.heston import PathBundle, simulate
from exo.models.params import EngineConfig, HestonParams
from exo.models.rng import RandomSource, SobolRandomSource
from exo.products.base import CashflowLedger, Payoff


def discount(ledger: CashflowLedger, *, r: float) -> NDArray[np.float64]:
    """Discount every leg of `ledger` at flat rate `r`, summed per path -> `(n_paths,)`."""
    result: NDArray[np.float64] = ledger.amounts @ np.exp(-r * ledger.t)
    return result


def price_from_bundle(
    payoff: Payoff,
    bundle: PathBundle,
    *,
    r: float,
    control: ControlVariate | None = None,
    beta: float | None = None,
) -> PriceResult:
    """Price `payoff` against an already-simulated `bundle`, discounting at flat rate `r`.

    Row order is preserved end to end: `payoff.cashflows` returns a ledger indexed by
    `bundle`'s own path order, `discount` reduces it to one `(n_paths,)` array without
    reordering, and that array is handed to `mc_estimate(bundle, ...)` unchanged -- the
    antithetic pair-mean standard error is positional and would silently mispair otherwise.
    `control` is applied to this already-discounted vector, elementwise, before
    `mc_estimate`, so that contract is untouched.

    # ponytail: `beta` is supplied from outside, never fitted on the priced sample, and
    # carries no degrees-of-freedom correction (none is valid under RQMC -- see
    # `estimate_beta`'s docstring). Ceiling: beta is only as good as whatever produced it --
    # a poor beta costs variance, never bias. Upgrade: a larger pilot sample. Trigger: the
    # study showing a CV ratio below ~1.2 at a cell with high payoff/control correlation.

    Requires `bundle.t[-1] == payoff.expiry` EXACTLY, for the same reason `price()` requires
    `engine.expiry == payoff.expiry` exactly (see `price()`'s docstring) -- `bundle.t[-1]` is
    `np.linspace`'s endpoint, set to the exact `expiry` it was simulated with, not a value that
    could pick up floating-point drift. This guard exists so the *pricer-mediated* path fails
    loud instead of silently pricing the wrong contract; `barrier.py`/`autocallable.py`
    independently bound their own monitoring/terminal-leg indices to the payoff's own expiry
    (not the bundle's last column), so a caller that calls `payoff.cashflows(bundle)` directly,
    bypassing this guard, is still priced correctly.

    # ponytail (P2-8, P2.M1 slice 2): the exact `bundle.t[-1] != payoff.expiry` guard means this
    # function cannot price a shorter-dated payoff against a shared longer bundle -- the use
    # case this docstring's opening paragraph advertises for P2.M4's portfolio revaluation.
    # Every current shorter-dated caller must bypass `price_from_bundle` and call
    # `discount(payoff.cashflows(bundle), r=...)` directly. Trigger: P2.M4, whose portfolio
    # revaluation decides whether to relax this guard. See docs/PONYTAIL-DEBT.md.
    """
    if bundle.t[-1] != payoff.expiry:
        raise ValueError(
            f"bundle horizon {bundle.t[-1]} != payoff.expiry {payoff.expiry} -- pricing a "
            "payoff against a bundle simulated to a different horizon would silently price "
            "the wrong contract."
        )
    if control is not None and beta is None:
        raise ValueError(
            "price_from_bundle: beta is required when control is supplied -- beta must come "
            "from a disjoint pilot sample (estimate_beta), never fitted on the sample being "
            "priced. See estimate_beta's docstring."
        )
    ledger = payoff.cashflows(bundle)
    discounted = discount(ledger, r=r)
    if control is not None:
        assert beta is not None  # guarded above; narrows for mypy
        discounted = apply_control(discounted, control, beta)
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


def price_rqmc(
    payoff: Payoff,
    params: HestonParams,
    engine: EngineConfig,
    *,
    seed: int,
    n_replicates: int,
    bridge: frozenset[str] = frozenset(),
    control_strike: float | None = None,
    beta: float | None = None,
) -> PriceResult:
    """Price `payoff` under RQMC (scrambled-Sobol) with `n_replicates` independent scrambles.

    Builds one `SobolRandomSource`/`PathBundle` per replicate, prices it, and discards it before
    building the next -- ONE bundle is ever live at a time, never a list of them (Slice 3 T4).
    Each replicate's scalar price is handed to `rqmc_estimate`, which reports the between-
    replicate standard error (see its docstring for the calibration this implies).

    `dims` is derived from what `simulate()` draws today for BOTH schemes -- `("variance",
    n_steps)` then `("spot", n_steps)` (heston.py) -- not from `engine.scheme` itself; see
    `SobolRandomSource.dims`'s own ponytail marker for the ceiling this shares.

    This ordering puts a bridged "spot" stream at global Sobol dimensions `[n_steps,
    2*n_steps)`, not `[0, n_steps)` -- `bridge.py`'s leading-dimensions claim applies only
    within a stream's own columns, not across this tuple (see its docstring). Ordering is not
    currently chosen for bridge effectiveness: reversing it was measured immaterial (rmse
    0.0370 vs 0.0383, 0.0801 vs 0.0670, 0.0451 vs 0.0442 -- mixed, within +/-15% precision), so
    it is left as-is rather than churning every study number for no measured gain. A future
    third stream should not assume ordering here is chosen for bridge effectiveness.

    Control variate: when `control_strike` is given and `beta` is not, ONE extra pilot replicate
    is drawn at `replicate=n_replicates` -- disjoint from the `0..n_replicates-1` estimation
    replicates -- fits `beta` there via `estimate_beta`, and discards that replicate's own price.
    The same `beta` is then applied to every estimation replicate (`control_variate.py`: a fixed
    beta is unbiased regardless of source; fitting it on the priced sample is not). Pass `beta`
    explicitly to skip the pilot.

    CAVEAT: `vanilla_control`'s `mean` is the exact continuous-time CF price, not the
    discretized `simulate()` model's own expectation, so this control variate silently transfers
    the scheme's discretization bias into the priced result rather than merely reducing variance
    -- see `estimate_beta`'s docstring and `vanilla_control`'s ponytail marker for the measured
    gap (e.g. +0.07189, z=+3.22, at euler-ft/n_steps=50, AAPL params). Do not enable
    `control_strike` under a scheme/step count not validated by ADR-006 Amendment 4 Section 4.
    """
    if engine.antithetic:
        raise ValueError(
            "price_rqmc requires engine.antithetic=False (ADR-006 Amendment 6): antithetic "
            "pairing of a scrambled Sobol net destroys the (t, m, s)-net equidistribution RQMC "
            "relies on."
        )
    if engine.n_paths <= 0 or (engine.n_paths & (engine.n_paths - 1)) != 0:
        raise ValueError(
            f"price_rqmc requires engine.n_paths to be a power of two, got {engine.n_paths}"
        )
    if engine.expiry != payoff.expiry:
        raise ValueError(
            f"EngineConfig.expiry={engine.expiry} does not match payoff.expiry="
            f"{payoff.expiry} -- these must match exactly, or the payoff would be priced "
            "against the wrong contract horizon."
        )
    if n_replicates < _MIN_RQMC_REPLICATES:
        raise ValueError(
            f"price_rqmc needs at least {_MIN_RQMC_REPLICATES} replicates (see "
            f"rqmc_estimate's docstring), got n_replicates={n_replicates}"
        )
    if beta is not None and control_strike is None:
        raise ValueError(
            "price_rqmc: beta was given but control_strike is None -- beta has nothing to "
            "apply to without a control"
        )

    dims: tuple[tuple[str, int], ...] = (("variance", engine.n_steps), ("spot", engine.n_steps))
    t: NDArray[np.float64] | None = None
    if bridge:
        t = np.linspace(0.0, engine.expiry, engine.n_steps + 1, dtype=np.float64)

    resolved_beta = beta
    if control_strike is not None and resolved_beta is None:
        pilot_rng = SobolRandomSource(
            seed=seed, n_paths=engine.n_paths, dims=dims, replicate=n_replicates, bridge=bridge, t=t
        )
        pilot_bundle = simulate(params, engine, pilot_rng)
        pilot_discounted = discount(payoff.cashflows(pilot_bundle), r=params.r)
        pilot_control = vanilla_control(
            pilot_bundle, params, strike=control_strike, expiry=payoff.expiry, r=params.r
        )
        resolved_beta = estimate_beta(pilot_discounted, pilot_control.payoff)

    replicate_pvs: list[float] = []
    for i in range(n_replicates):
        rng = SobolRandomSource(
            seed=seed, n_paths=engine.n_paths, dims=dims, replicate=i, bridge=bridge, t=t
        )
        bundle = simulate(params, engine, rng)
        discounted = discount(payoff.cashflows(bundle), r=params.r)
        if control_strike is not None:
            assert resolved_beta is not None  # resolved above whenever control_strike is set
            control = vanilla_control(
                bundle, params, strike=control_strike, expiry=payoff.expiry, r=params.r
            )
            discounted = apply_control(discounted, control, resolved_beta)
        replicate_pvs.append(float(discounted.mean()))

    return rqmc_estimate(replicate_pvs, n_paths_per_replicate=engine.n_paths)
