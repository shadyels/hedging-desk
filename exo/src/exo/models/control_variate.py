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
#
# ponytail: `mean` is the EXACT (continuous-time CF) price, not the DISCRETIZED model's own
# expectation of `payoff` -- see `estimate_beta`'s docstring for why this makes `apply_control`
# transfer the discretization bias into every controlled estimate rather than merely costing
# variance. Ceiling: measured directly (400k antithetic pseudo paths, seed 999, AAPL params,
# K=s0=187.50, T=1) -- E_disc[control] vs CF exact: qe/50 gives -0.01432 (z=-0.67), euler-ft/50
# gives +0.07189 (z=+3.22), qe/400 gives -0.00278 (z=-0.13). Upgrade: none -- fixing this means
# simulating the control on a disjoint sample to get a discretized-model mean, which is a
# design change (see `estimate_beta`'s "Rejected alternatives"), not a remediation. Trigger:
# P2.M4 publishing a controlled number, or any use of this control under a scheme/step count
# not validated by ADR-006 Amendment 4 Section 4.
def vanilla_control(
    bundle: PathBundle,
    params: HestonParamsLike,
    *,
    strike: float,
    expiry: float,
    r: float,
) -> ControlVariate:
    """Heston vanilla CALL control on the bundle's terminal spot (`bundle.S[:, -1]`).

    `mean` is `heston_vanilla_price`'s EXACT (continuous-time, characteristic-function) price,
    not the discretized `simulate()` model's own expectation of `payoff` -- the two differ by
    the scheme's discretization bias. See `estimate_beta`'s docstring for the consequence this
    has for `apply_control`, and the ponytail marker above for the measured gap.
    """
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

    A control variate with ANY fixed beta is unbiased FOR THE DISCRETIZED MODEL'S OWN
    EXPECTATION, PROVIDED `control.mean` equals that same discretized model's expectation of
    `control.payoff` -- only then does a poor beta cost variance and never bias. `vanilla_control`
    does NOT supply that: its `mean` is the exact continuous-time CF price, not
    `simulate()`'s discretized expectation, so `apply_control` additionally transfers
    `beta * (E_disc[control] - E_exact[control])` into every controlled estimate -- it silently
    REPAIRS discretization bias rather than reporting it. Measured (400k antithetic pseudo
    paths, seed 999, AAPL params, K=s0=187.50, T=1): qe/50 bias -0.01432 (z=-0.67), euler-ft/50
    bias +0.07189 (z=+3.22), qe/400 bias -0.00278 (z=-0.13) -- i.e. this would mask exactly the
    euler-ft/50 defect `tests/test_validation_gates_qmc.py`'s module docstring independently
    established. See `vanilla_control`'s ponytail marker; no fix is applied here (using a
    discretized control mean requires simulating the control separately and is a design change,
    not a remediation -- ADR-006 Amendment 4 Section 4).

    So beta must come from OUTSIDE the sample being priced -- a dedicated pilot scramble
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
