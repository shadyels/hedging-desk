"""See exo/CLAUDE.md for this package's role and rules.

Public re-exports for the numerics core built in P2.M1 slice 1: Heston parameters and engine
config, the RNG seam, path simulation, MC estimation, and the closed-form/characteristic-function
reference pricers used as this slice's blocking validation gates.
"""

from __future__ import annotations

from exo.models.analytic import bs_call_price, bs_put_price
from exo.models.estimator import PriceResult, mc_estimate
from exo.models.heston import PathBundle, simulate
from exo.models.heston_cf import heston_vanilla_price
from exo.models.params import EngineConfig, HestonParams
from exo.models.rng import PseudoRandomSource, RandomSource

__all__ = [
    "EngineConfig",
    "HestonParams",
    "PathBundle",
    "PriceResult",
    "PseudoRandomSource",
    "RandomSource",
    "bs_call_price",
    "bs_put_price",
    "heston_vanilla_price",
    "mc_estimate",
    "simulate",
]
