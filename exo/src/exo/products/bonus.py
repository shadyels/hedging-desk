"""Bonus certificate: a term sheet plus the `Payoff` it defines.

One cashflow at `expiry`: `ratio * (S_T + surv * max(bonus_level - S_T, 0))`. While the DOWN
barrier is never touched the holder receives at least the bonus level; once touched it is the
plain underlying. PV = ratio * (S0 e^{-qT} + down-and-out put(K = bonus_level, H = barrier)).

# ponytail: no cap. Ceiling: cannot price a capped bonus certificate. Trigger: first real
# term sheet with a cap. See docs/PONYTAIL-DEBT.md.
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
    validate_observation_schedule,
)


@dataclass(frozen=True)
class BonusCertificate:
    """`observations` iff `monitoring == DISCRETE`. No `s0` field: the caller must ensure the
    barrier is not already breached at inception."""

    underlying: str
    ratio: float
    bonus_level: float
    barrier: float
    expiry: float
    monitoring: Monitoring
    observations: tuple[float, ...] | None = None

    def __post_init__(self) -> None:
        # NaN/Inf bypass every `<=`/`<` guard under IEEE-754 -- checked explicitly.
        if not math.isfinite(self.ratio) or self.ratio <= 0.0:
            raise ValueError(f"ratio must be a positive finite number, got {self.ratio}")
        if not math.isfinite(self.bonus_level) or self.bonus_level <= 0.0:
            raise ValueError(
                f"bonus_level must be a positive finite number, got {self.bonus_level}"
            )
        if not math.isfinite(self.barrier) or self.barrier <= 0.0:
            raise ValueError(f"barrier must be a positive finite number, got {self.barrier}")
        if self.barrier >= self.bonus_level:
            raise ValueError(
                f"barrier ({self.barrier}) must be below bonus_level ({self.bonus_level})"
            )
        if not math.isfinite(self.expiry) or self.expiry <= 0.0:
            raise ValueError(f"expiry must be a positive finite number, got {self.expiry}")
        if not isinstance(self.monitoring, Monitoring):
            raise ValueError(f"monitoring must be a Monitoring, got {self.monitoring!r}")
        if self.monitoring == Monitoring.DISCRETE:
            if self.observations is None:
                raise ValueError("observations is required when monitoring is DISCRETE")
            validate_observation_schedule(self.observations, self.expiry)
        elif self.observations is not None:
            raise ValueError(
                "observations must be None when monitoring is CONTINUOUS_BRIDGE, got "
                f"{self.observations}"
            )

    def cashflows(self, bundle: PathBundle, *, r: float) -> CashflowLedger:
        surv, expiry_idx = expiry_survival(
            bundle, self.barrier, "down", self.monitoring, self.observations, self.expiry
        )
        s_t = bundle.S[:, expiry_idx]
        amounts = self.ratio * (s_t + surv * np.maximum(self.bonus_level - s_t, 0.0))
        return CashflowLedger(t=np.array([self.expiry]), amounts=amounts.reshape(-1, 1))
