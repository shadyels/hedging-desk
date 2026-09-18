"""Single-barrier European option: a term sheet plus the `Payoff` it defines.

A single terminal cashflow at `expiry`: `w * max(+-(S_T - K), 0)` where `w = survival` for
knock-out and `w = 1 - survival` for knock-in (`base.barrier_survival`). This makes in-out
parity (`KO + KI == vanilla`) hold EXACTLY, path by path -- a free machine-precision test
(`exo/tests/test_validation_gates_products.py`'s G5 gate), since the same `survival` value
feeds both weights and `w_ko + w_ki == 1` identically.

# ponytail (P2-5, P2.M1 slice 2): no rebate on knock-out. Ceiling: cannot price a barrier with
# a rebate feature. Trigger: P2.M2 (same trigger as base.py's crossing-time marker).
# See docs/PONYTAIL-DEBT.md.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import numpy as np

from exo.models.heston import PathBundle
from exo.products.base import (
    _DIRECTIONS,  # the one products/ copy of the direction vocabulary
    CashflowLedger,
    Monitoring,
    barrier_survival,
    observation_indices,
    validate_observation_schedule,
)

_OPTION_TYPES = ("call", "put")
_KNOCKS = ("out", "in")


@dataclass(frozen=True)
class BarrierOption:
    """A single-barrier European option term sheet.

    `observations` is required iff `monitoring == Monitoring.DISCRETE` (the declared fixing
    schedule) and must be `None` for `Monitoring.CONTINUOUS_BRIDGE` (which monitors every grid
    step from inception up to this option's OWN expiry, not the bundle's full horizon -- see
    `cashflows()` below and `base.barrier_survival`).

    This term sheet has no `s0` field (spot lives in the pricing-time `HestonParams`, not
    here), so it cannot itself check whether `barrier` is already breached relative to the
    underlying's actual starting level -- the caller is responsible for that before pricing.
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
        # Categorical fields are consumed through two-way branches, never an explicit
        # elif/raise, so a case typo (e.g. "Call") would otherwise be silently accepted and
        # priced as the OTHER branch.
        if self.option_type not in _OPTION_TYPES:
            raise ValueError(
                f"option_type must be one of {_OPTION_TYPES}, got {self.option_type!r}"
            )
        if self.direction not in _DIRECTIONS:
            raise ValueError(f"direction must be one of {_DIRECTIONS}, got {self.direction!r}")
        if self.knock not in _KNOCKS:
            raise ValueError(f"knock must be one of {_KNOCKS}, got {self.knock!r}")

        # NaN/Inf bypass every `<=`/`<`/`>` guard below under IEEE-754 -- checked explicitly
        # alongside each range check, not left to fall out of it.
        if not math.isfinite(self.strike) or self.strike <= 0.0:
            raise ValueError(f"strike must be a positive finite number, got {self.strike}")
        if not math.isfinite(self.barrier) or self.barrier <= 0.0:
            raise ValueError(f"barrier must be a positive finite number, got {self.barrier}")
        if not math.isfinite(self.expiry) or self.expiry <= 0.0:
            raise ValueError(f"expiry must be a positive finite number, got {self.expiry}")

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
        expiry_idx = int(observation_indices(np.array([self.expiry]), bundle)[0])

        if self.monitoring == Monitoring.DISCRETE:
            assert self.observations is not None  # __post_init__ guarantees this for DISCRETE
            obs_idx = observation_indices(np.asarray(self.observations, dtype=np.float64), bundle)
        else:
            # Indices are bound to THIS option's own expiry, not the bundle's full horizon --
            # `price_from_bundle` lets several products share one bundle, so a bundle simulated
            # further out than this option's expiry must not have its extra steps monitored (or
            # read as the terminal spot below) as if they belonged to this contract.
            obs_idx = np.arange(expiry_idx + 1)

        survival = barrier_survival(bundle, self.barrier, self.direction, self.monitoring, obs_idx)
        weight = survival if self.knock == "out" else 1.0 - survival

        s_t = bundle.S[:, expiry_idx]
        if self.option_type == "call":
            intrinsic = np.maximum(s_t - self.strike, 0.0)
        else:
            intrinsic = np.maximum(self.strike - s_t, 0.0)

        amounts = (weight * intrinsic).reshape(-1, 1)
        return CashflowLedger(t=np.array([self.expiry]), amounts=amounts)
