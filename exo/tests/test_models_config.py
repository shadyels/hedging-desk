"""Tests for `exo.toml`'s `[models]` section and its loader.

Covers: the real `exo.toml` file parses into `(ModelDefaults, dict[str, HestonParams])`;
the four equity underlyings from `protocol/refdata/universe.json` (AAPL, MSFT, NESN, SPX) are
all present; feller_ratio is computed correctly; AT LEAST ONE configured set violates the
Feller condition (2*kappa*theta/xi**2 < 1) and AT LEAST ONE satisfies it — a comment in
exo.toml does not survive a well-meaning config tweak, a test does. Also re-checks (in this
config-focused module) that pydantic rejects an invalid rho, a non-positive kappa, and an
unknown scheme, per the P2.M1 slice 1 spec's explicit ask for TRACK C's own coverage of these.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from exo.models import EngineConfig, HestonParams
from exo.models.params import ModelDefaults, load_models_section

_EXO_TOML = Path(__file__).resolve().parents[1] / "exo.toml"

# The equity underlyings this slice configures, per protocol/refdata/universe.json
# instrument_ids 1001 (AAPL), 1002 (MSFT), 1003 (NESN, CHF) plus the SPX index (2001).
_EXPECTED_SYMBOLS = {"AAPL", "MSFT", "NESN", "SPX"}


def _load_real_models_section() -> tuple[ModelDefaults, dict[str, HestonParams]]:
    raw = tomllib.loads(_EXO_TOML.read_text())
    return load_models_section(raw["models"])


def test_exo_toml_models_section_parses() -> None:
    defaults, params = _load_real_models_section()
    assert defaults.n_steps_per_year > 0
    assert set(params) == _EXPECTED_SYMBOLS


def test_exo_toml_configures_all_four_universe_underlyings() -> None:
    _, params = _load_real_models_section()
    for symbol in _EXPECTED_SYMBOLS:
        assert symbol in params, f"exo.toml [models.{symbol}] is missing"


def test_at_least_one_configured_set_violates_feller() -> None:
    """2*kappa*theta/xi**2 < 1: the regime where QE and full-truncation Euler
    actually differ, and therefore the regime the chosen scheme must be shown to
    survive (docs/studies/p2m1-scheme-convergence.md)."""
    _, params = _load_real_models_section()
    violating = [symbol for symbol, p in params.items() if p.feller_ratio < 1.0]
    assert violating, "at least one exo.toml [models.*] set must violate the Feller condition"


def test_at_least_one_configured_set_satisfies_feller() -> None:
    _, params = _load_real_models_section()
    satisfying = [symbol for symbol, p in params.items() if p.feller_ratio >= 1.0]
    assert satisfying, "at least one exo.toml [models.*] set must satisfy the Feller condition"


def test_nesn_is_chf_denominated_illustrative_params() -> None:
    """NESN (instrument_id 1003) is CHF in protocol/refdata/universe.json; its
    illustrative r/q should be plausible for CHF (near-zero short rate, a
    meaningfully positive dividend yield), not a copy-pasted USD block."""
    _, params = _load_real_models_section()
    nesn = params["NESN"]
    assert 0.0 <= nesn.r < 0.02
    assert nesn.q > 0.0


def test_feller_ratio_matches_formula() -> None:
    p = HestonParams(s0=100.0, r=0.0, q=0.0, v0=0.04, kappa=1.5, theta=0.05, xi=0.6, rho=-0.5)
    assert p.feller_ratio == pytest.approx(2 * 1.5 * 0.05 / 0.6**2)


def test_heston_params_rejects_rho_outside_open_unit_interval() -> None:
    with pytest.raises(ValidationError):
        HestonParams(s0=100.0, r=0.0, q=0.0, v0=0.04, kappa=1.0, theta=0.04, xi=0.5, rho=1.5)


def test_heston_params_rejects_non_positive_kappa() -> None:
    with pytest.raises(ValidationError):
        HestonParams(s0=100.0, r=0.0, q=0.0, v0=0.04, kappa=0.0, theta=0.04, xi=0.5, rho=-0.5)


def test_engine_config_rejects_unknown_scheme() -> None:
    with pytest.raises(ValidationError):
        EngineConfig(scheme="tree", n_steps=10, n_paths=100, expiry=1.0, antithetic=True)  # type: ignore[arg-type]
