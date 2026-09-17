"""See exo/CLAUDE.md for this package's role and rules.

Public re-exports for the payoff abstraction and the two products built against it in P2.M1
slice 2: the undiscounted dated cashflow ledger, the barrier-monitoring vocabulary, the
Brownian-bridge survival weight, the MC pricer, and the barrier option / autocallable term
sheets.
"""

from __future__ import annotations

from exo.products.autocallable import Autocallable
from exo.products.barrier import BarrierOption
from exo.products.base import (
    CashflowLedger,
    Monitoring,
    Payoff,
    ScheduleAlignmentError,
    barrier_survival,
    observation_indices,
    validate_observation_schedule,
)
from exo.products.pricer import discount, price, price_from_bundle

__all__ = [
    "Autocallable",
    "BarrierOption",
    "CashflowLedger",
    "Monitoring",
    "Payoff",
    "ScheduleAlignmentError",
    "barrier_survival",
    "discount",
    "observation_indices",
    "price",
    "price_from_bundle",
    "validate_observation_schedule",
]
