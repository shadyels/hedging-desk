"""Control variate for barrier options: the Heston vanilla, priced in closed form.

ADR-006 Amendment 4: there is no closed form for a barrier under Heston, so the natural
control is the Heston vanilla, priced by `heston_cf.heston_vanilla_price` -- the same
characteristic-function pricer that already serves as the slice's blocking validation gate
(exo/CLAUDE.md engine constraint 5 put it in `models/` precisely so this module could reuse
it instead of writing it twice).

Lives in `models/`, not `products/`: it needs only `bundle.S[:, -1]` and `HestonParams`, so
`products -> models` layering is preserved, never the reverse.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from exo.models.heston import PathBundle
from exo.models.heston_cf import HestonParamsLike, heston_vanilla_price


@dataclass(frozen=True, eq=False)
class ControlVariate:
    """A control's discounted per-path payoff (bundle row order, positional) and its exact
    mean under the same model parameters. `eq=False`: an `NDArray` field has no meaningful
    `==`, and dataclass equality is never used for this type."""

    payoff: NDArray[np.float64]
    mean: float


# ponytail: call controls only -- no put control, no put-call parity conversion -- and
# barrier products only. Ceiling: down-and-out/in CALLS get variance reduction; put barriers
# and the autocallable get none. Upgrade: one parity line (`vanilla_price = call - S*exp(-q*T)
# + K*exp(-r*T)`) makes a put control trivial; the autocallable needs a different control
# entirely (no closed form for its coupon/redemption structure). Trigger: a put-barrier gate,
# or P2.M3's products.
def vanilla_control(
    bundle: PathBundle,
    params: HestonParamsLike,
    *,
    strike: float,
    expiry: float,
    r: float,
) -> ControlVariate:
    """Heston vanilla CALL control on the bundle's terminal spot (`bundle.S[:, -1]`)."""
    payoff = np.exp(-r * expiry) * np.maximum(bundle.S[:, -1] - strike, 0.0)
    mean = heston_vanilla_price(params, strike, expiry, is_call=True)
    return ControlVariate(payoff=payoff, mean=mean)


def estimate_beta(payoff: NDArray[np.float64], control: NDArray[np.float64]) -> float:
    """OLS slope Cov(payoff, control) / Var(control), for use ONLY on a sample disjoint from
    the one being priced.

    `price_from_bundle` requires `beta` to be supplied, never fits it on the priced sample.
    Reason (the same one `PriceResult.combine`'s docstring already records for a different
    mechanism): a beta estimated from the same draws it then weights reintroduces exactly the
    estimated-weight bias `combine` rejects. The textbook `n-2` degrees-of-freedom correction
    for an estimated beta assumes INDEPENDENT samples, which randomized-QMC draws are not --
    so under RQMC there is no correct small-sample correction to fall back on either.

    A control variate with ANY fixed beta is unbiased; a poor beta only costs variance, never
    bias. So beta must come from OUTSIDE the sample being priced -- a dedicated pilot scramble
    replicate, disjoint from the estimation replicates (T4's `price_rqmc`). This function is
    that pilot's estimator, not something `price_from_bundle` may call on its own inputs.

    Rejected alternatives:
    (a) same-sample OLS beta -- reintroduces the same-data-estimated-weight bias above.
    (b) `beta = 1` -- unbiased and needs no pilot machinery, but gives up variance reduction
        whenever `2*Cov(payoff, control) < Var(control)`.
    """
    control_mean = control.mean()
    var = float(np.mean((control - control_mean) ** 2))
    if var == 0.0:
        raise ValueError("estimate_beta: control has zero variance, cannot compute OLS slope")
    cov = float(np.mean((payoff - payoff.mean()) * (control - control_mean)))
    return cov / var


def apply_control(
    payoff: NDArray[np.float64], control: ControlVariate, beta: float
) -> NDArray[np.float64]:
    """`payoff - beta * (control.payoff - control.mean)`, elementwise, in bundle row order."""
    if payoff.shape != control.payoff.shape:
        raise ValueError(
            f"apply_control: payoff has shape {payoff.shape} but control.payoff has shape "
            f"{control.payoff.shape}"
        )
    result: NDArray[np.float64] = payoff - beta * (control.payoff - control.mean)
    return result
