"""The payoff abstraction: an undiscounted, dated cashflow ledger.

Slice 1 shipped no `Payoff` abstraction -- every gate test inlined its payoff, deliberately,
because an abstraction designed against zero real products is an abstraction designed against
nothing (ADR-006 Amendment 4 s3). This module and the two products built against it in the same
pass (`products/barrier.py`, `products/autocallable.py`) are that abstraction's first real load.

WHY UNDISCOUNTED AND DATED, NOT A DISCOUNTED SCALAR PER PATH: early redemption (autocallable),
coupon strips, TARF accumulation (P2.M3) and mini-future residual-value settlement on stop-out
(P2.M2) all become "a cashflow on a different date" under this shape rather than four bespoke
payoff mechanisms. Discounting is centralized in `products/pricer.py` and tested once there.
Critically, keeping `r` OUT of the payoff means a payoff's shape does not change when P2.M4 bumps
`r` for rho -- a payoff that discounted internally would need its own bump-awareness.

The `Payoff` Protocol takes a single `PathBundle`. `exo/CLAUDE.md`'s M2 abstraction rule: if a new
payoff requires touching `models/`, the abstraction is wrong. Every payoff-specific mechanism
here -- the observation-to-grid-index mapping, the Brownian-bridge survival weight -- lives in
THIS module (a function of a `PathBundle` that a product's `cashflows()` calls), not in
`models/`, precisely so new products stay a `products/` change.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal, Protocol

import numpy as np
from numpy.typing import NDArray

from exo.models.heston import PathBundle


class Monitoring(str, Enum):
    """How a barrier (or any other level-crossing condition) is monitored against a
    `PathBundle`'s simulated steps.

    `CONTINUOUS_BRIDGE` uses a Brownian-bridge per-step survival probability (see
    `barrier_survival`) -- what P2.M2's mini future needs (continuous knock-out).
    `DISCRETE` checks a declared observation schedule with a hard indicator -- what
    P2.M3's TARF needs (monthly fixings). Building both now, each exercised by a real
    product (`products/barrier.py`), is the minimal spanning set (brief s2).
    """

    CONTINUOUS_BRIDGE = "continuous_bridge"
    DISCRETE = "discrete"


class ScheduleAlignmentError(ValueError):
    """An observation schedule does not land on the `PathBundle`'s simulation grid.

    Silently snapping an observation to the nearest step changes the price, so misalignment is
    a hard error here rather than a fixup -- matching this repo's posture (e.g. `cross_px_policy`
    / `[tracker]`) of failing loud on configuration that cannot be honoured.
    """


@dataclass(frozen=True)
class CashflowLedger:
    """Per-path, per-observation cashflows in product currency, UNDISCOUNTED.

    `t`: payment times in years, ascending, shape `(n_obs,)`.
    `amounts`: shape `(n_paths, n_obs)`. Row order MUST match the `PathBundle` the payoff was
    computed against -- `estimator.py`'s antithetic pair-mean standard error is POSITIONAL
    (row i is the antithetic twin of row i + n_pairs), so a ledger that reorders rows silently
    mispairs it.
    """

    t: NDArray[np.float64]
    amounts: NDArray[np.float64]


class Payoff(Protocol):
    """A priceable product: something that turns one `PathBundle` into a `CashflowLedger`.

    Single-underlying by design in P2.M1 -- `cashflows` takes exactly one `PathBundle`. See the
    ponytail marker below for the P4.M5 multi-asset (worst-of) upgrade this defers.

    # ponytail (P2-3, P2.M1 slice 2): `cashflows` takes a single `PathBundle`, so a payoff can
    # only reference one underlying's simulated path. Ceiling: no multi-asset product (a
    # worst-of autocallable, a basket barrier) can be expressed against this Protocol as-is.
    # Trigger: P4.M5 multi-asset work -- likely widens the signature to a mapping of underlying
    # -> PathBundle (all sharing one PathBundle.t grid) rather than reworking every M1/M2/M3
    # single-asset product.
    """

    @property
    def expiry(self) -> float: ...

    def cashflows(self, bundle: PathBundle) -> CashflowLedger: ...


def observation_indices(times: NDArray[np.float64], bundle: PathBundle) -> NDArray[np.intp]:
    """Map observation times onto `bundle.t` grid indices.

    Raises `ScheduleAlignmentError` unless every observation lands on a grid point within
    `1e-9 * expiry` (`expiry = bundle.t[-1]`), and rejects any observation at `t == 0` (t=0 is
    the pricing date, not an observable payment/fixing date). Silently snapping to the nearest
    step changes the price, so misalignment is a hard error (see `ScheduleAlignmentError`).
    """
    expiry = float(bundle.t[-1])
    tol = 1e-9 * expiry
    times_arr = np.asarray(times, dtype=np.float64)

    if np.any(times_arr <= 0.0):
        raise ScheduleAlignmentError(
            f"observation times must be > 0 (t=0 is the pricing date, not observable), got "
            f"{times_arr.tolist()}"
        )

    grid = bundle.t
    right = np.clip(np.searchsorted(grid, times_arr), 1, grid.shape[0] - 1)
    left = right - 1
    dist_right = np.abs(grid[right] - times_arr)
    dist_left = np.abs(grid[left] - times_arr)
    nearest = np.where(dist_left <= dist_right, left, right)
    nearest_dist = np.minimum(dist_left, dist_right)

    if np.any(nearest_dist > tol):
        bad = times_arr[nearest_dist > tol]
        raise ScheduleAlignmentError(
            f"observation time(s) {bad.tolist()} do not land on a PathBundle grid point "
            f"within tol={tol} (expiry={expiry}, n_steps={grid.shape[0] - 1})"
        )
    return nearest.astype(np.intp)


def barrier_survival(
    bundle: PathBundle,
    level: float,
    direction: Literal["down", "up"],
    monitoring: Monitoring,
    obs_idx: NDArray[np.intp],
) -> NDArray[np.float64]:
    """Per-path survival probability (never breaching `level`) over `obs_idx`, shape
    `(n_paths,)`, every entry in `[0, 1]`.

    For `DISCRETE`, survival is the hard indicator that no observation in `obs_idx` breached.

    For `CONTINUOUS_BRIDGE`, `obs_idx` must cover the full simulation grid. Conditional on the
    simulated endpoints of a step, the probability a driftless Brownian bridge touched the
    barrier during that step is known in closed form (Glasserman, *Monte Carlo Methods in
    Financial Engineering* s6.4):

        x_i     = ln(S_i     / H)
        x_{i+1} = ln(S_{i+1} / H)
        p_cross_i = exp(-2 * x_i * x_{i+1} / (v_i * dt))   # same expression, up or down
        w_survive = prod_i (1 - p_cross_i)                 # and 0 on any breached ENDPOINT

    Three guards, all required (brief s4.1):
      - `v_i` is clamped at 0 before use ("euler-ft" permits a negative variance STATE);
      - where `v_i * dt <= 0`, `p_cross` is set to 0 (no diffusion in that step -> the bridge
        cannot cross between two un-breached endpoints);
      - a path whose ENDPOINT already breached short-circuits to survival 0 exactly.

    # ponytail (P2-1, P2.M1 slice 2): survival is a PROBABILITY, carrying no crossing TIME, so
    # a knock-out here cannot emit a dated rebate cashflow (there is no "when" to date it at).
    # Ceiling: no rebate-bearing product can be built on this function as-is. Trigger: P2.M2's
    # mini future, which settles at residual value on stop-out and needs the stop-out date --
    # likely needs an expected-first-crossing-time estimator alongside (not instead of) this
    # survival weight.
    # ponytail (P2-2, P2.M1 slice 2): the step's variance is taken at the LEFT point,
    # `v_i = max(bundle.v[:, i], 0)` -- exact under the BS-degenerate limit this module's own
    # gate (G4) validates against, but an approximation once vol is genuinely stochastic within
    # a step. Ceiling: accuracy is unverified outside the degenerate limit. Trigger: P4.M4's PDE
    # cross-check, which can quantify the approximation's bias under real Heston parameters.
    """
    s = bundle.S
    v = bundle.v

    obs_s = s[:, obs_idx]
    breached = obs_s <= level if direction == "down" else obs_s >= level
    endpoint_breach = np.any(breached, axis=1)

    if monitoring == Monitoring.DISCRETE:
        return np.where(endpoint_breach, 0.0, 1.0)

    idx_a = obs_idx[:-1]
    idx_b = obs_idx[1:]
    s_a = s[:, idx_a]
    s_b = s[:, idx_b]
    v_a = np.maximum(v[:, idx_a], 0.0)
    dt = (bundle.t[idx_b] - bundle.t[idx_a])[np.newaxis, :]

    x_a = np.log(s_a / level)
    x_b = np.log(s_b / level)
    denom = v_a * dt
    diffusive = denom > 0.0
    safe_denom = np.where(diffusive, denom, 1.0)
    # A pair straddling the barrier (one endpoint breached, the other not) can drive this
    # exponent to a large positive value that overflows to +inf -- harmless, since such a pair
    # always belongs to a path caught by `endpoint_breach` below and its p_cross is discarded,
    # never left to silently saturate at 1.0 for a path that should survive.
    with np.errstate(over="ignore"):
        p_cross = np.where(diffusive, np.exp(-2.0 * x_a * x_b / safe_denom), 0.0)
    p_cross = np.clip(p_cross, 0.0, 1.0)

    survive_product = np.prod(1.0 - p_cross, axis=1)
    survival = np.where(endpoint_breach, 0.0, survive_product)
    return np.clip(survival, 0.0, 1.0)
