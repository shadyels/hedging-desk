"""Reverse convertible (RC) and barrier reverse convertible (BRC): one term sheet, one `Payoff`.

Coupon leg: `notional * coupon_rate` on every `coupon_times` date (one ledger column each, so
each is discounted at its own date -- G-RC1 pins that). The last column also carries the
terminal leg:

- RC (`barrier is None`): `notional * min(1, S_T / strike)` -- the holder is short a put
  struck at `strike` (conversion into `notional / strike` units when `S_T < strike`).
- BRC: `notional * (surv + (1 - surv) * min(1, S_T / strike))` where `surv` is the survival
  probability of the DOWN barrier up to this note's own expiry (`base.expiry_survival`). The
  put only knocks in if the barrier was touched, so BRC - RC = (N / K) * surv * (K - S_T)^+
  per path, exactly (G-BRC-par).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from exo.models.heston import PathBundle
from exo.products.base import (
    CashflowLedger,
    Monitoring,
    expiry_survival,
    observation_indices,
    validate_observation_schedule,
)


@dataclass(frozen=True)
class ReverseConvertible:
    """RC when `barrier is None`, BRC otherwise.

    `monitoring` is required iff `barrier` is set; `observations` iff `monitoring` is
    DISCRETE. `coupon_times` must end at `expiry`. The term sheet has no `s0`, so the caller
    is responsible for `barrier` not already being breached at inception.
    """

    underlying: str
    notional: float
    strike: float
    coupon_rate: float
    coupon_times: tuple[float, ...]
    expiry: float
    barrier: float | None = None
    monitoring: Monitoring | None = None
    observations: tuple[float, ...] | None = None

    def __post_init__(self) -> None:
        # NaN/Inf bypass every `<=`/`<` guard under IEEE-754 -- checked explicitly.
        if not math.isfinite(self.notional) or self.notional <= 0.0:
            raise ValueError(f"notional must be a positive finite number, got {self.notional}")
        if not math.isfinite(self.strike) or self.strike <= 0.0:
            raise ValueError(f"strike must be a positive finite number, got {self.strike}")
        if not math.isfinite(self.coupon_rate) or self.coupon_rate < 0.0:
            raise ValueError(
                f"coupon_rate must be a non-negative finite number, got {self.coupon_rate}"
            )
        if not math.isfinite(self.expiry) or self.expiry <= 0.0:
            raise ValueError(f"expiry must be a positive finite number, got {self.expiry}")
        validate_observation_schedule(self.coupon_times, self.expiry, require_terminal=True)

        if self.barrier is None:
            if self.monitoring is not None or self.observations is not None:
                raise ValueError("monitoring and observations must be None when barrier is None")
            return

        if not math.isfinite(self.barrier) or self.barrier <= 0.0:
            raise ValueError(f"barrier must be a positive finite number, got {self.barrier}")
        if self.barrier >= self.strike:
            raise ValueError(f"barrier ({self.barrier}) must be below strike ({self.strike})")
        # A typo'd or missing monitoring would otherwise be read through a two-way branch.
        if not isinstance(self.monitoring, Monitoring):
            raise ValueError(
                f"monitoring must be a Monitoring when barrier is set, got {self.monitoring!r}"
            )
        if self.monitoring == Monitoring.DISCRETE:
            if self.observations is None:
                raise ValueError("observations is required when monitoring is DISCRETE")
            validate_observation_schedule(self.observations, self.expiry)
        elif self.observations is not None:
            raise ValueError(
                "observations must be None when monitoring is CONTINUOUS_BRIDGE, got "
                f"{self.observations}"
            )

    def cashflows(self, bundle: PathBundle) -> CashflowLedger:
        coupon_idx = observation_indices(np.asarray(self.coupon_times, dtype=np.float64), bundle)
        n_paths = bundle.S.shape[0]
        amounts = np.full(
            (n_paths, len(self.coupon_times)), self.notional * self.coupon_rate, dtype=np.float64
        )

        # Terminal spot is read at THIS note's own expiry index, never the bundle's last column
        # (a shared bundle may run past `expiry`). coupon_times[-1] == expiry within tolerance.
        if self.barrier is None:
            s_t = bundle.S[:, coupon_idx[-1]]
            surv = None
        else:
            assert self.monitoring is not None  # __post_init__ guarantees this when barrier set
            surv, expiry_idx = expiry_survival(
                bundle, self.barrier, "down", self.monitoring, self.observations, self.expiry
            )
            s_t = bundle.S[:, expiry_idx]

        converted = np.minimum(1.0, s_t / self.strike)
        terminal = converted if surv is None else surv + (1.0 - surv) * converted
        amounts[:, -1] += self.notional * terminal
        return CashflowLedger(t=np.asarray(self.coupon_times, dtype=np.float64), amounts=amounts)
