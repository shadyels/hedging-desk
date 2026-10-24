"""TARF and PTARF (EURUSD target redemption forwards): term sheets plus the `Payoff` they define.

Notation: `S_j` is EURUSD spot (USD per EUR) at fixing `t_j`; `N` is the notional in EUR per
fixing. All amounts are USD. Sign convention is CLIENT-SIDE (holder): positive = the client
receives; desk PV is the negation.

TARF, per fixing:  gain_j = N * max(S_j - K, 0);  loss_j = L * N * max(K - S_j, 0).

PTARF (market pivot `P`), per fixing:
  gain_j = N * (1{S_j < P} * max(S_j - K_lo, 0) + 1{S_j >= P} * max(K_hi - S_j, 0))
  loss_j = L * N * (1{S_j < P} * max(K_lo - S_j, 0) + 1{S_j >= P} * max(S_j - K_hi, 0))
A tie `S_j == P` goes to the UPPER regime. Gains from both regimes count toward one target, one
leverage `L` applies on both losing sides, and asymmetric strikes are allowed (no symmetry check).

The client's cashflow at fixing `j` is `paid_j - loss_j`, where `paid_j = gain_j` until the
accumulated gain reaches the target `G`. Knock-out rule: breach uses `>=` (`acc + gain_j >= G`);
the breaching fixing pays the capped remainder `G - acc`, so total gains equal the target
exactly, and every later column is exactly 0. Losses never count toward the target and never
knock out. The ledger is UNDISCOUNTED and dated at the fixings `t_j`; discounting happens in
`pricer.discount`.

# ponytail: FX rides HestonParams with q := r_f (ADR-006 A8 s1). Ceiling: nothing in the type
# marks q as a rate, so a P2.M4 bump of q is EUR (foreign) rho for TARF/PTARF but "div sens" for
# equities (ADR-008), and ADR-009's demo rho aggregation has one USD bucket only. Upgrade: owner
# P2.M4 (Greek publication); labelling/bucketing mechanism undecided. See docs/PONYTAIL-DEBT.md.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from exo.models.heston import PathBundle
from exo.products.base import CashflowLedger, observation_indices, validate_observation_schedule

# ponytail: cashflows are dated at the fixing t_j, not t_j + refdata's fx_settlement_lag_days (2).
# Ceiling: each leg discounted ~2 days short (|c_j|*r_d*2/365). Upgrade: date column j at t_j +
# lag (CashflowLedger.t need not sit on the grid); needs a day-count/calendar for the lag, which
# refdata does not define -- owner undecided. See docs/PONYTAIL-DEBT.md.


def _target_redemption(
    gain: NDArray[np.float64], loss: NDArray[np.float64], target: float
) -> NDArray[np.float64]:
    """Client amounts `(n_paths, n_fix)` for a target-redemption structure.

    Float-drift guards (all three are load-bearing):

    (a) the knock-out state is the `alive` boolean, never re-derived from `acc >= target`;
    (b) on breach `acc` is assigned the literal `target`, not `acc + paid`;
    (c) the breach test and the accumulation use the same float expression `acc + g`, so
        `alive` implies `acc < target`.

    Losses never count toward the target and never knock out. After knock-out every later
    column is 0.
    """
    n_paths, n_fix = gain.shape
    amounts = np.zeros((n_paths, n_fix), dtype=np.float64)
    acc: NDArray[np.float64] = np.zeros(n_paths, dtype=np.float64)
    alive: NDArray[np.bool_] = np.ones(n_paths, dtype=np.bool_)
    for j in range(n_fix):
        g_j = gain[:, j]
        l_j = loss[:, j]
        new = acc + g_j
        breach = alive & (new >= target)
        paid = np.where(breach, target - acc, g_j)
        amounts[:, j] = np.where(alive, paid - l_j, 0.0)
        acc = np.where(breach, target, np.where(alive, new, acc))
        alive = alive & ~breach
    return amounts


def _check_positive(**fields: float) -> None:
    # NaN/Inf bypass `<=` under IEEE-754, so finiteness is checked explicitly.
    for name, value in fields.items():
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError(f"{name} must be a positive finite number, got {value}")


@dataclass(frozen=True)
class Tarf:
    """TARF term sheet (see the module docstring for the payoff and sign convention)."""

    underlying: str
    notional: float
    strike: float
    leverage: float
    target: float
    fixings: tuple[float, ...]
    expiry: float

    def __post_init__(self) -> None:
        _check_positive(
            notional=self.notional,
            strike=self.strike,
            leverage=self.leverage,
            target=self.target,
            expiry=self.expiry,
        )
        validate_observation_schedule(self.fixings, self.expiry, require_terminal=True)

    def cashflows(self, bundle: PathBundle, *, r: float) -> CashflowLedger:
        """Undiscounted client ledger dated at the fixings. `r` is accepted and IGNORED
        (discounting happens in `pricer.discount`)."""
        t = np.asarray(self.fixings, dtype=np.float64)
        s = bundle.S[:, observation_indices(t, bundle)]
        gain = self.notional * np.maximum(s - self.strike, 0.0)
        loss = self.leverage * self.notional * np.maximum(self.strike - s, 0.0)
        return CashflowLedger(t=t, amounts=_target_redemption(gain, loss, self.target))


@dataclass(frozen=True)
class Ptarf:
    """PTARF term sheet with a market pivot (see the module docstring)."""

    underlying: str
    notional: float
    strike_low: float
    pivot: float
    strike_high: float
    leverage: float
    target: float
    fixings: tuple[float, ...]
    expiry: float

    def __post_init__(self) -> None:
        _check_positive(
            notional=self.notional,
            strike_low=self.strike_low,
            pivot=self.pivot,
            strike_high=self.strike_high,
            leverage=self.leverage,
            target=self.target,
            expiry=self.expiry,
        )
        if not self.strike_low < self.pivot < self.strike_high:
            raise ValueError(
                f"require strike_low < pivot < strike_high, got "
                f"{self.strike_low}, {self.pivot}, {self.strike_high}"
            )
        validate_observation_schedule(self.fixings, self.expiry, require_terminal=True)

    def cashflows(self, bundle: PathBundle, *, r: float) -> CashflowLedger:
        """Undiscounted client ledger dated at the fixings. `r` is accepted and IGNORED
        (discounting happens in `pricer.discount`)."""
        t = np.asarray(self.fixings, dtype=np.float64)
        s = bundle.S[:, observation_indices(t, bundle)]
        upper = s >= self.pivot  # tie goes to the upper regime
        gain = self.notional * np.where(
            upper,
            np.maximum(self.strike_high - s, 0.0),
            np.maximum(s - self.strike_low, 0.0),
        )
        loss = (
            self.leverage
            * self.notional
            * np.where(
                upper,
                np.maximum(s - self.strike_high, 0.0),
                np.maximum(self.strike_low - s, 0.0),
            )
        )
        return CashflowLedger(t=t, amounts=_target_redemption(gain, loss, self.target))
