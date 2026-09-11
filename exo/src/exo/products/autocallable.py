"""Phoenix-style autocallable note: a term sheet plus the `Payoff` it defines.

Chosen (over the barrier option alone) because it applies the most abstraction pressure per
line: early redemption, a genuine path-dependent state accumulator (the memory coupon), and a
terminal leg that is "in addition to" an observation-date coupon all have to fall out of one
dated `CashflowLedger` rather than three bespoke mechanisms.

Setting `coupon_barrier == autocall_trigger` in a term sheet recovers snowball-note behaviour
(coupon and autocall fire together, always) -- one implementation covers both shapes.

The memory accumulator is the direct rehearsal for P2.M3's TARF accumulated-gain knockout
state: both are "a per-path counter that resets on one condition and is read by another."
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from exo.models.heston import PathBundle
from exo.products.base import CashflowLedger, observation_indices


@dataclass(frozen=True)
class Autocallable:
    """Phoenix autocallable note term sheet.

    At each observation `j` (schedule order):

    - Coupon: paid if `S_j >= coupon_barrier`, amount `notional * coupon_rate`. With
      `memory=True`, a paid coupon also releases every previously missed coupon since the
      last payment: `notional * coupon_rate * (1 + n_missed_since_last_payment)`.
    - Autocall: the note redeems early if `S_j >= autocall_trigger`, paying `notional` IN
      ADDITION TO that observation's own coupon. After redemption the path emits NO further
      cashflows -- every later ledger entry for that path is exactly `0.0`.
    - Maturity (only for paths never called): pays `notional` if `S_T >= protection_barrier`,
      else `notional * S_T / initial_level`. This is IN ADDITION TO any maturity-date coupon.

    `initial_level` is S0. `observations[-1]` must equal `expiry` -- the maturity leg is paid
    alongside the final observation's coupon/autocall check, not on a separate date.
    """

    underlying: str
    notional: float
    observations: tuple[float, ...]
    autocall_trigger: float
    coupon_barrier: float
    coupon_rate: float
    memory: bool
    protection_barrier: float
    initial_level: float
    expiry: float

    def __post_init__(self) -> None:
        if self.notional <= 0.0:
            raise ValueError(f"notional must be positive, got {self.notional}")
        if self.expiry <= 0.0:
            raise ValueError(f"expiry must be positive, got {self.expiry}")
        if self.autocall_trigger <= 0.0 or self.coupon_barrier <= 0.0:
            raise ValueError(
                "autocall_trigger and coupon_barrier must be positive, got "
                f"{self.autocall_trigger}, {self.coupon_barrier}"
            )
        if self.protection_barrier <= 0.0:
            raise ValueError(f"protection_barrier must be positive, got {self.protection_barrier}")
        if self.initial_level <= 0.0:
            raise ValueError(f"initial_level must be positive, got {self.initial_level}")
        if self.coupon_rate < 0.0:
            raise ValueError(f"coupon_rate must be non-negative, got {self.coupon_rate}")
        if len(self.observations) == 0:
            raise ValueError("observations must be non-empty")
        if any(o <= 0.0 or o > self.expiry for o in self.observations):
            raise ValueError(
                f"observations must lie within (0, expiry={self.expiry}], got {self.observations}"
            )
        if list(self.observations) != sorted(self.observations) or len(
            set(self.observations)
        ) != len(self.observations):
            raise ValueError(f"observations must be strictly ascending, got {self.observations}")
        if self.observations[-1] != self.expiry:
            raise ValueError(
                f"the last observation ({self.observations[-1]}) must equal expiry "
                f"({self.expiry}) -- the maturity leg is paid alongside the final "
                "observation's coupon check"
            )

    def cashflows(self, bundle: PathBundle) -> CashflowLedger:
        obs_idx = observation_indices(np.asarray(self.observations, dtype=np.float64), bundle)
        n_paths = bundle.S.shape[0]
        n_obs = len(self.observations)

        amounts = np.zeros((n_paths, n_obs), dtype=np.float64)
        called: NDArray[np.bool_] = np.zeros(n_paths, dtype=np.bool_)
        missed_since_payment: NDArray[np.float64] = np.zeros(n_paths, dtype=np.float64)

        for j in range(n_obs):
            s_j = bundle.S[:, obs_idx[j]]
            active = ~called
            coupon_hit = s_j >= self.coupon_barrier
            autocall_hit = s_j >= self.autocall_trigger

            n_missed = missed_since_payment if self.memory else np.zeros(n_paths)
            coupon_amt = self.notional * self.coupon_rate * (1.0 + n_missed)

            cf = np.where(coupon_hit, coupon_amt, 0.0)
            cf = cf + np.where(autocall_hit, self.notional, 0.0)
            amounts[:, j] = np.where(active, cf, 0.0)

            missed_since_payment = np.where(
                active,
                np.where(coupon_hit, 0.0, missed_since_payment + 1.0),
                missed_since_payment,
            )
            called = called | (active & autocall_hit)

        never_called = ~called
        s_t = bundle.S[:, -1]
        maturity = np.where(
            s_t >= self.protection_barrier,
            self.notional,
            self.notional * s_t / self.initial_level,
        )
        amounts[:, -1] += np.where(never_called, maturity, 0.0)

        return CashflowLedger(t=np.asarray(self.observations, dtype=np.float64), amounts=amounts)
