"""Warrant (call or put, European or American): a term sheet plus the `Payoff` it defines.

European: one cashflow at `expiry`, `ratio * max(+-(S_T - K), 0)` (PV = ratio * vanilla).
American exercise needs the Longstaff-Schwartz engine (`price_american`, P2.M2 slice 3), which
is a pricer, not a ledger -- `cashflows()` therefore RAISES for it rather than silently pricing
an American warrant as European.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from exo.models.heston import PathBundle
from exo.products.base import CashflowLedger, observation_indices

_OPTION_TYPES = ("call", "put")
_EXERCISES = ("european", "american")


@dataclass(frozen=True)
class Warrant:
    underlying: str
    option_type: Literal["call", "put"]
    strike: float
    expiry: float
    ratio: float
    exercise: Literal["european", "american"]

    def __post_init__(self) -> None:
        # Categorical fields feed two-way branches, so a typo would silently price the other one.
        if self.option_type not in _OPTION_TYPES:
            raise ValueError(
                f"option_type must be one of {_OPTION_TYPES}, got {self.option_type!r}"
            )
        if self.exercise not in _EXERCISES:
            raise ValueError(f"exercise must be one of {_EXERCISES}, got {self.exercise!r}")
        # NaN/Inf bypass every `<=` guard under IEEE-754 -- checked explicitly.
        if not math.isfinite(self.strike) or self.strike <= 0.0:
            raise ValueError(f"strike must be a positive finite number, got {self.strike}")
        if not math.isfinite(self.expiry) or self.expiry <= 0.0:
            raise ValueError(f"expiry must be a positive finite number, got {self.expiry}")
        if not math.isfinite(self.ratio) or self.ratio <= 0.0:
            raise ValueError(f"ratio must be a positive finite number, got {self.ratio}")

    def intrinsic(self, s: NDArray[np.float64]) -> NDArray[np.float64]:
        """`ratio * max(+-(s - K), 0)` elementwise."""
        sign = 1.0 if self.option_type == "call" else -1.0
        out: NDArray[np.float64] = self.ratio * np.maximum(sign * (s - self.strike), 0.0)
        return out

    def cashflows(self, bundle: PathBundle) -> CashflowLedger:
        if self.exercise == "american":
            raise ValueError(
                "American warrants have no European cashflow ledger -- price them with "
                "price_american (Longstaff-Schwartz), never via price()/cashflows()"
            )
        expiry_idx = int(observation_indices(np.array([self.expiry]), bundle)[0])
        amounts = self.intrinsic(bundle.S[:, expiry_idx]).reshape(-1, 1)
        return CashflowLedger(t=np.array([self.expiry]), amounts=amounts)
