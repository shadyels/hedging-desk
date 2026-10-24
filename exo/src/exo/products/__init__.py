"""See exo/CLAUDE.md for this package's role and rules.

Public re-exports for the payoff abstraction and the products built against it: the
undiscounted dated cashflow ledger, the barrier-monitoring vocabulary, the Brownian-bridge
survival weight, the MC pricer, and the term sheets (barrier option, autocallable, reverse
convertible / BRC, bonus certificate, warrant, mini future, TARF / PTARF).
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
    bridge_step_survival,
    expiry_survival,
    observation_indices,
    validate_observation_schedule,
)
from exo.products.bonus import BonusCertificate
from exo.products.mini_future import MiniFuture
from exo.products.pricer import (
    LowerBoundPriceResult,
    discount,
    price,
    price_american,
    price_from_bundle,
)
from exo.products.reverse_convertible import ReverseConvertible
from exo.products.tarf import Ptarf, Tarf
from exo.products.warrant import Warrant

__all__ = [
    "Autocallable",
    "BarrierOption",
    "BonusCertificate",
    "CashflowLedger",
    "LowerBoundPriceResult",
    "MiniFuture",
    "Monitoring",
    "Payoff",
    "Ptarf",
    "ReverseConvertible",
    "ScheduleAlignmentError",
    "Tarf",
    "Warrant",
    "barrier_survival",
    "bridge_step_survival",
    "discount",
    "expiry_survival",
    "observation_indices",
    "price",
    "price_american",
    "price_from_bundle",
    "validate_observation_schedule",
]
