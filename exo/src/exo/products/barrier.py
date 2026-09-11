"""Single-barrier European option: a term sheet plus the `Payoff` it defines.

A single terminal cashflow at `expiry`: `w * max(+-(S_T - K), 0)` where `w = survival` for
knock-out and `w = 1 - survival` for knock-in (`base.barrier_survival`). This makes in-out
parity (`KO + KI == vanilla`) hold EXACTLY, path by path -- a free machine-precision test
(`exo/tests/test_validation_gates_products.py`'s G5 gate), since the same `survival` value
feeds both weights and `w_ko + w_ki == 1` identically.

# ponytail (P2-5, P2.M1 slice 2): no rebate on knock-out -- a breached path simply pays 0 at
# expiry rather than a consolation cashflow at the breach date. Ceiling: cannot price a barrier
# with a rebate feature. Trigger: P2.M2, same trigger as `base.py`'s survival-carries-no-
# crossing-time marker (a rebate needs the crossing date this module also does not have).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

from exo.models.heston import PathBundle
from exo.products.base import CashflowLedger, Monitoring, barrier_survival, observation_indices


@dataclass(frozen=True)
class BarrierOption:
    """A single-barrier European option term sheet.

    `observations` is required iff `monitoring == Monitoring.DISCRETE` (the declared fixing
    schedule) and must be `None` for `Monitoring.CONTINUOUS_BRIDGE` (which monitors the full
    simulation grid instead -- see `base.barrier_survival`).
    """

    underlying: str
    option_type: Literal["call", "put"]
    strike: float
    expiry: float
    barrier: float
    direction: Literal["down", "up"]
    knock: Literal["out", "in"]
    monitoring: Monitoring
    observations: tuple[float, ...] | None = None

    def __post_init__(self) -> None:
        if self.strike <= 0.0:
            raise ValueError(f"strike must be positive, got {self.strike}")
        if self.barrier <= 0.0:
            raise ValueError(f"barrier must be positive, got {self.barrier}")
        if self.expiry <= 0.0:
            raise ValueError(f"expiry must be positive, got {self.expiry}")

        if self.monitoring == Monitoring.DISCRETE:
            if self.observations is None:
                raise ValueError("observations is required when monitoring is DISCRETE")
            obs = self.observations
            if any(o <= 0.0 or o > self.expiry for o in obs):
                raise ValueError(
                    f"observations must lie within (0, expiry={self.expiry}], got {obs}"
                )
            if list(obs) != sorted(obs):
                raise ValueError(f"observations must be strictly ascending, got {obs}")
            if len(set(obs)) != len(obs):
                raise ValueError(f"observations must not repeat, got {obs}")
        elif self.observations is not None:
            raise ValueError(
                "observations must be None when monitoring is CONTINUOUS_BRIDGE, got "
                f"{self.observations}"
            )

    def cashflows(self, bundle: PathBundle) -> CashflowLedger:
        if self.monitoring == Monitoring.DISCRETE:
            assert self.observations is not None  # enforced in __post_init__
            obs_idx = observation_indices(np.asarray(self.observations, dtype=np.float64), bundle)
        else:
            obs_idx = np.arange(bundle.t.shape[0])

        survival = barrier_survival(bundle, self.barrier, self.direction, self.monitoring, obs_idx)
        weight = survival if self.knock == "out" else 1.0 - survival

        s_t = bundle.S[:, -1]
        if self.option_type == "call":
            intrinsic = np.maximum(s_t - self.strike, 0.0)
        else:
            intrinsic = np.maximum(self.strike - s_t, 0.0)

        amounts = (weight * intrinsic).reshape(-1, 1)
        return CashflowLedger(t=np.array([self.expiry]), amounts=amounts)
