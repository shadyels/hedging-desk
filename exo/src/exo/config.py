"""The whole of `exo.toml` as one validated, immutable model (exo/CLAUDE.md: a single `exo.toml`
loaded at startup; no env-var spelunking in business logic).

Every section is `extra="forbid"`: a typo'd key is an error, never a silently dropped setting.
`[models]` goes through the existing `load_models_section`; `[hedger]` and `[rho_transfer]` are
typed passthrough for their P4 consumers (ADR-009) and are validated only as `str -> number`.

Relative paths in `[valuation]` resolve against the EXO PROJECT ROOT (`exo/`), never against the
config file's location or the CWD, so a harness can run a COPIED config from anywhere.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Self

from pydantic import ConfigDict, Field, StrictBool, field_validator, model_validator

from exo.greeks.bump import BumpConfig
from exo.models.params import (
    HestonParams,
    ModelDefaults,
    StrictNumbersModel,
    load_models_section,
)
from exo.refdata import load_universe

# exo/src/exo/config.py -> exo/
EXO_ROOT = Path(__file__).resolve().parents[2]


def _resolve(path: Path) -> Path:
    return path if path.is_absolute() else (EXO_ROOT / path).resolve()


class _Section(StrictNumbersModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class BusConfig(_Section):
    nats_url: str = Field(min_length=1)
    max_clock_skew_s: float = Field(
        gt=0
    )  # replay guards reject sent_ns this far ahead (ADR-014 s15)


class ValuationConfig(_Section):
    revaluation_interval_s: float = Field(gt=0)  # interval-republish cadence (ADR-014 s9)
    book: Path
    refdata: Path
    manifest_dir: Path
    max_job_bytes: int = Field(gt=0)  # load-time memory guard (ADR-006 Amendment 9 s7)
    min_revaluation_gap_s: float = Field(
        gt=0
    )  # per book, from the previous job's END (ADR-014 s15)
    retry_after_s: float = Field(gt=0)  # a failed book is retried after this, or on a changed mark
    job_timeout_s: float = Field(gt=0)  # a longer job is FATAL: EXO reports ok=false and exits

    @field_validator("book", "refdata", "manifest_dir")
    @classmethod
    def _root_relative(cls, value: Path) -> Path:
        return _resolve(value)

    @model_validator(mode="after")
    def _retry_not_faster_than_pacing(self) -> Self:
        if self.retry_after_s < self.min_revaluation_gap_s:
            raise ValueError("[valuation] retry_after_s must be >= min_revaluation_gap_s")
        return self


class ModelsConfig(_Section):
    defaults: ModelDefaults
    params: dict[str, HestonParams]

    @model_validator(mode="after")
    def _antithetic_required(self) -> Self:
        # Published numbers use pseudo-random + antithetic only (ADR-006 Amendment 9 s1).
        if not self.defaults.antithetic:
            raise ValueError("[models] antithetic must be true (ADR-006 Amendment 9 s1)")
        return self


class RehedgeConfig(_Section):
    band_equity_qty_e2: int = Field(gt=0)
    band_fx_qty_e2: int = Field(gt=0)


class ReconConfig(_Section):
    tolerance_qty_e2: int = Field(ge=0)
    grace_ms: int = Field(ge=0)
    max_snapshot_gap_ms: int = Field(gt=0)


class ProvenanceConfig(_Section):
    allow_dirty: StrictBool


class ExoConfig(_Section):
    bus: BusConfig
    valuation: ValuationConfig
    models: ModelsConfig
    rehedge: RehedgeConfig
    greeks: BumpConfig
    recon: ReconConfig
    provenance: ProvenanceConfig
    # ponytail: passthrough dicts, lax-mode (bool coerces to a number); nothing reads them in P2.M4.
    # Upgrade: typed StrictNumbersModel sections when ADR-009's hedger / rho transfer (P4)
    # consumes them.
    hedger: dict[str, int | float]
    rho_transfer: dict[str, int | float]


def load_config(path: Path) -> ExoConfig:
    """Parse and validate the whole `exo.toml`.

    Beyond the schema: every `[models.<SYM>]` must be a symbol in the refdata universe
    (ADR-006 Amendment 9 s7), so a typo like `[models.APPL]` is an error rather than a
    silently unused entry.
    """
    raw = tomllib.loads(path.read_text())
    if "models" not in raw:
        raise ValueError("exo.toml has no [models] section")
    defaults, params = load_models_section(raw.pop("models"))
    cfg = ExoConfig.model_validate({**raw, "models": {"defaults": defaults, "params": params}})
    universe = load_universe(cfg.valuation.refdata)
    unknown = sorted(s for s in cfg.models.params if universe.instrument_by_symbol(s) is None)
    if unknown:
        raise ValueError(f"[models.<SYMBOL>] not in the refdata universe: {unknown}")
    return cfg
