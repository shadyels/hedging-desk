"""Validated model configuration: Heston parameters, engine settings, and the
`exo.toml` `[models]` loader.

Pydantic (not a frozen dataclass) per exo/CLAUDE.md and root CLAUDE.md's ground
rules: these are LOADED, VALIDATED config, not internal value objects computed by
the engine. Every model here is immutable (`frozen=True`) once constructed and
rejects unknown keys (`extra="forbid"`, P2 code review 2026-09-06): without it, a
typo'd `exo.toml` key (e.g. `rh0 = -0.7` instead of `rho = -0.7`) is silently
DROPPED rather than raising -- the ponytail in `load_models_section` below covers
only unrecognized SYMBOL sections, not typo'd keys within a recognized one.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Scheme = Literal["qe", "euler-ft"]


class HestonParams(BaseModel):
    """Heston (1993) model parameters for a single underlying.

    s0: spot. r, q: continuously-compounded risk-free rate and dividend yield
    (unconstrained in sign — negative rates are real). v0: initial variance.
    kappa: mean-reversion speed. theta: long-run variance. xi: vol-of-vol.
    rho: spot/variance correlation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    s0: float = Field(gt=0)
    r: float
    q: float
    v0: float = Field(gt=0)
    kappa: float = Field(gt=0)
    theta: float = Field(gt=0)
    xi: float = Field(gt=0)
    rho: float = Field(gt=-1, lt=1)

    @property
    def feller_ratio(self) -> float:
        """2*kappa*theta / xi**2. Below 1, variance can (in the continuous-time
        limit) reach zero — the regime the discretization scheme choice must be
        shown to survive (see docs/studies/p2m1-scheme-convergence.md)."""
        return 2.0 * self.kappa * self.theta / self.xi**2


class EngineConfig(BaseModel):
    """Monte Carlo engine settings for one `simulate()` call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scheme: Scheme
    n_steps: int = Field(gt=0)
    n_paths: int = Field(gt=0)
    expiry: float = Field(gt=0)
    antithetic: bool = True

    @model_validator(mode="after")
    def _n_paths_even_when_antithetic(self) -> EngineConfig:
        if self.antithetic and self.n_paths % 2 != 0:
            raise ValueError(
                f"n_paths must be even when antithetic=True, got n_paths={self.n_paths}"
            )
        return self


class ModelDefaults(BaseModel):
    """The flat keys of `exo.toml`'s `[models]` table: engine defaults shared
    across underlyings, before any per-underlying `[models.<SYMBOL>]` override."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scheme: Scheme
    n_steps_per_year: int = Field(gt=0)
    antithetic: bool = True


def load_models_section(
    models: Mapping[str, object],
) -> tuple[ModelDefaults, dict[str, HestonParams]]:
    """Parse a TOML `[models]` table into (defaults, {symbol: HestonParams}).

    `models` is the already-parsed `[models]` table (e.g. `tomllib.loads(...)["models"]`):
    flat keys are engine defaults; each `Mapping`-valued key is a per-underlying
    `[models.<SYMBOL>]` subtable.

    ponytail: subtable keys (the `<SYMBOL>` in `[models.<SYMBOL>]`) are not
    cross-checked against `protocol/refdata/universe.json`, so a typo in exo.toml
    (e.g. `[models.APPL]`) parses cleanly into a silently-unused HestonParams entry
    that nothing in this slice ever looks up. Ceiling: no refdata cross-check.
    Trigger: P2.M4, when ValuationSnapshot needs InstrumentRef (id, class, currency)
    from refdata anyway — validate symbols against it there.
    """
    flat = {key: value for key, value in models.items() if not isinstance(value, Mapping)}
    defaults = ModelDefaults.model_validate(flat)
    params = {
        symbol: HestonParams.model_validate(table)
        for symbol, table in models.items()
        if isinstance(table, Mapping)
    }
    return defaults, params
