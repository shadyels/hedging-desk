"""Shared fixtures for the P2.M4 slice-2 tests: the real `exo.toml`/universe, one loadable term
sheet per class (observation dates on the 50- and 100-step grids), and a book-document builder."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from exo.config import ExoConfig, load_config
from exo.products.autocallable import Autocallable
from exo.products.barrier import BarrierOption
from exo.products.base import Monitoring
from exo.products.bonus import BonusCertificate
from exo.products.reverse_convertible import ReverseConvertible
from exo.products.tarf import Ptarf, Tarf
from exo.products.warrant import Warrant
from exo.refdata import Universe, load_universe

EXO_DIR = Path(__file__).resolve().parents[1]
EXO_TOML = EXO_DIR / "exo.toml"
BOOK_TOML = EXO_DIR / "book.toml"

_OBS = (0.2, 0.4, 0.6, 0.8, 1.0)  # on both the 50- and the 100-step grids over expiry 1.0
_DISCRETE_OBS = tuple(i / 10 for i in range(1, 11))

# class name -> (term sheet, underlying symbol); expiry 1.0 everywhere.
LOADABLE: dict[str, tuple[Any, str]] = {
    "autocallable": (
        Autocallable(
            underlying="AAPL",
            notional=1e6,
            observations=_OBS,
            autocall_trigger=187.5,
            coupon_barrier=178.125,
            coupon_rate=0.02,
            memory=True,
            protection_barrier=131.25,
            initial_level=187.5,
            expiry=1.0,
        ),
        "AAPL",
    ),
    "barrier_continuous": (
        BarrierOption(
            underlying="AAPL",
            option_type="call",
            strike=187.5,
            expiry=1.0,
            barrier=150.0,
            direction="down",
            knock="out",
            monitoring=Monitoring.CONTINUOUS_BRIDGE,
        ),
        "AAPL",
    ),
    "barrier_discrete": (
        BarrierOption(
            underlying="AAPL",
            option_type="put",
            strike=187.5,
            expiry=1.0,
            barrier=220.0,
            direction="up",
            knock="in",
            monitoring=Monitoring.DISCRETE,
            observations=_DISCRETE_OBS,
        ),
        "AAPL",
    ),
    "rc": (
        ReverseConvertible(
            underlying="AAPL",
            notional=1e6,
            strike=187.5,
            coupon_rate=0.02,
            coupon_times=_OBS,
            expiry=1.0,
        ),
        "AAPL",
    ),
    "brc_continuous": (
        ReverseConvertible(
            underlying="AAPL",
            notional=1e6,
            strike=187.5,
            coupon_rate=0.02,
            coupon_times=_OBS,
            expiry=1.0,
            barrier=131.25,
            monitoring=Monitoring.CONTINUOUS_BRIDGE,
        ),
        "AAPL",
    ),
    "brc_discrete": (
        ReverseConvertible(
            underlying="AAPL",
            notional=1e6,
            strike=187.5,
            coupon_rate=0.02,
            coupon_times=_OBS,
            expiry=1.0,
            barrier=131.25,
            monitoring=Monitoring.DISCRETE,
            observations=_DISCRETE_OBS,
        ),
        "AAPL",
    ),
    "bonus_continuous": (
        BonusCertificate(
            underlying="AAPL",
            ratio=1.0,
            bonus_level=210.0,
            barrier=140.0,
            expiry=1.0,
            monitoring=Monitoring.CONTINUOUS_BRIDGE,
        ),
        "AAPL",
    ),
    "bonus_discrete": (
        BonusCertificate(
            underlying="AAPL",
            ratio=1.0,
            bonus_level=210.0,
            barrier=140.0,
            expiry=1.0,
            monitoring=Monitoring.DISCRETE,
            observations=_DISCRETE_OBS,
        ),
        "AAPL",
    ),
    "warrant_call": (
        Warrant(
            underlying="AAPL",
            option_type="call",
            strike=187.5,
            expiry=1.0,
            ratio=1.0,
            exercise="european",
        ),
        "AAPL",
    ),
    "tarf": (
        Tarf(
            underlying="EURUSD",
            notional=1e6,
            strike=1.08,
            leverage=2.0,
            target=60_000.0,
            fixings=_OBS,
            expiry=1.0,
        ),
        "EURUSD",
    ),
    "ptarf": (
        Ptarf(
            underlying="EURUSD",
            notional=1e6,
            strike_low=1.05,
            pivot=1.08,
            strike_high=1.11,
            leverage=2.0,
            target=60_000.0,
            fixings=_OBS,
            expiry=1.0,
        ),
        "EURUSD",
    ),
}


def real_config() -> ExoConfig:
    return load_config(EXO_TOML)


def real_universe(cfg: ExoConfig | None = None) -> Universe:
    return load_universe((cfg or real_config()).valuation.refdata)


def storyline_product_doc(**overrides: object) -> dict[str, Any]:
    """The `book.toml` autocallable as a parsed-TOML product table (with optional overrides)."""
    doc: dict[str, Any] = {
        "product_id": "EXO-SP-AC-0001",
        "type": "autocallable",
        "desk_quantity": -1.0,
        "underlying": "AAPL",
        "notional": 10_000_000.0,
        "observations": [0.25, 0.5, 0.75, 1.0],
        "autocall_trigger": 187.50,
        "coupon_barrier": 178.125,
        "coupon_rate": 0.02,
        "memory": True,
        "protection_barrier": 131.25,
        "initial_level": 187.50,
        "expiry": 1.0,
    }
    doc.update(overrides)
    return doc


def book_doc(*products: dict[str, Any], seed: int = 7, n_paths: int = 1000) -> dict[str, Any]:
    return {
        "book": [{"code": "EXO-SP", "seed": seed, "n_paths": n_paths, "product": list(products)}]
    }
