"""The whole-`exo.toml` loader (`exo.config`), the refdata reader, and the NaN guards of
`HestonParams` / `mc_estimate` (ADR-006 Amendment 9 s7)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from _book_fixtures import EXO_TOML, real_config

from exo.config import EXO_ROOT, load_config
from exo.models.estimator import mc_estimate
from exo.models.heston import simulate
from exo.models.params import EngineConfig, HestonParams
from exo.models.rng import PseudoRandomSource
from exo.refdata import load_universe

RAW = EXO_TOML.read_text()


def _variant(tmp_path: Path, old: str, new: str) -> Path:
    assert old in RAW, old
    path = tmp_path / "exo.toml"
    path.write_text(RAW.replace(old, new, 1))
    return path


def test_real_config_loads_every_section() -> None:
    cfg = real_config()
    assert cfg.bus.nats_url.startswith("nats://")
    assert set(cfg.models.params) == {"AAPL", "MSFT", "NESN", "SPX", "EURUSD"}
    assert cfg.models.defaults.antithetic
    assert cfg.greeks.bump_spot_rel == 0.01  # desk lead, 2026-10-24
    assert cfg.rehedge.band_equity_qty_e2 == 50_000
    assert (cfg.recon.tolerance_qty_e2, cfg.recon.grace_ms) == (0, 1500)
    assert cfg.recon.max_snapshot_gap_ms == 5000
    assert cfg.provenance.allow_dirty is False
    assert cfg.valuation.max_job_bytes == 1_000_000_000
    assert cfg.hedger["risk_weight_vega"] == 1.0
    assert "usd_bucket_threshold_e9" in cfg.rho_transfer


def test_relative_paths_resolve_against_the_exo_root_not_the_config_location(
    tmp_path: Path,
) -> None:
    copied = tmp_path / "copy" / "exo.toml"
    copied.parent.mkdir()
    copied.write_text(RAW)
    cfg = load_config(copied)
    assert cfg.valuation.book == EXO_ROOT / "book.toml"
    assert cfg.valuation.manifest_dir == EXO_ROOT / "run-manifests" / "live"
    assert cfg.valuation.refdata.name == "universe.json" and cfg.valuation.refdata.is_file()
    abs_path = tmp_path / "elsewhere.toml"
    cfg2 = load_config(_variant(tmp_path, 'book = "book.toml"', f'book = "{abs_path}"'))
    assert cfg2.valuation.book == abs_path


def test_models_symbol_typo_is_rejected_against_the_universe(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="APPL"):
        load_config(_variant(tmp_path, "[models.AAPL]", "[models.APPL]"))


def test_antithetic_must_be_true(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="antithetic must be true"):
        load_config(_variant(tmp_path, "antithetic = true", "antithetic = false"))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("[greeks]\n", "[greeks]\nbump_typo = 1.0\n"),
        ("[rehedge]\n", "[rehedge]\nband_extra = 1\n"),
        ("[valuation]\n", "[valuation]\ndefault_n_paths = 200000\n"),
        ("[provenance]\n", "[provenance]\nallow_dirt = true\n"),
        ("[bus]\n", "[bus]\nnats_urls = 'x'\n"),
        ("[recon]\n", "[recon]\ngrace = 1\n"),
        ("[rho_transfer]", "[rho_extra]\nx = 1\n\n[rho_transfer]"),
    ],
)
def test_unknown_keys_and_sections_are_rejected(tmp_path: Path, old: str, new: str) -> None:
    with pytest.raises(ValueError):
        load_config(_variant(tmp_path, old, new))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("grace_ms = 1500", "grace_ms = -1"),
        ("tolerance_qty_e2 = 0", "tolerance_qty_e2 = -1"),
        ("max_snapshot_gap_ms = 5000", "max_snapshot_gap_ms = 0"),
        ("allow_dirty = false", 'allow_dirty = "false"'),
        ("allow_dirty = false", "allow_dirty = 0"),
        ("bump_spot_rel = 0.01", "bump_spot_rel = 0.0"),
        ("bump_vol_abs = 0.005", "bump_vol_abs = -0.005"),
        ("max_job_bytes = 1_000_000_000", "max_job_bytes = 0"),
        ("band_equity_qty_e2 = 50_000", "band_equity_qty_e2 = 0"),
        ("revaluation_interval_s = 5 ", "revaluation_interval_s = 0 "),
    ],
)
def test_out_of_range_values_are_rejected(tmp_path: Path, old: str, new: str) -> None:
    with pytest.raises(ValueError):
        load_config(_variant(tmp_path, old, new))


def test_a_missing_section_is_rejected(tmp_path: Path) -> None:
    start = RAW.index("[recon]")
    end = RAW.index("[provenance]")
    path = tmp_path / "exo.toml"
    path.write_text(RAW[:start] + RAW[end:])
    with pytest.raises(ValueError, match="recon"):
        load_config(path)


def test_universe_reader_exposes_books_and_instruments() -> None:
    uni = load_universe(real_config().valuation.refdata)
    exo_sp = uni.book_by_code("EXO-SP")
    assert exo_sp is not None and exo_sp.book_id == 3
    aapl = uni.instrument_by_symbol("AAPL")
    assert aapl is not None and (aapl.instrument_id, aapl.lot_e2, aapl.currency) == (
        1001,
        100,
        "USD",
    )
    assert uni.instrument_by_id(4001) is not None
    assert uni.instrument_by_symbol("NOPE") is None
    assert len(uni.instruments) == 15


@pytest.mark.parametrize("field", ["s0", "r", "q", "v0", "kappa", "theta", "xi", "rho"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_heston_params_reject_non_finite(field: str, bad: float) -> None:
    good = {"s0": 100.0, "r": 0.02, "q": 0.01, "v0": 0.04, "kappa": 1.5, "theta": 0.04,
            "xi": 0.6, "rho": -0.5}  # fmt: skip
    with pytest.raises(ValueError):
        HestonParams.model_validate({**good, field: bad})


def test_mc_estimate_raises_on_a_non_finite_estimate() -> None:
    params = HestonParams(s0=100.0, r=0.02, q=0.0, v0=0.04, kappa=1.5, theta=0.04, xi=0.5, rho=-0.5)
    bundle = simulate(
        params,
        EngineConfig(scheme="qe", n_steps=5, n_paths=100, expiry=1.0, antithetic=True),
        PseudoRandomSource(1),
    )
    payoff = np.ones(100)
    mc_estimate(bundle, payoff)
    payoff[3] = np.nan
    with np.errstate(invalid="ignore"), pytest.raises(ValueError, match="non-finite"):
        mc_estimate(bundle, payoff)
    payoff[3] = np.inf
    with np.errstate(invalid="ignore"), pytest.raises(ValueError, match="non-finite"):
        mc_estimate(bundle, payoff)


# --- numeric fields reject bool and str (pydantic lax mode would coerce them) -------------

_NUMERIC_EDITS = [  # (old, replacement line prefix) one per section / model
    ("max_job_bytes = 1_000_000_000", "max_job_bytes"),  # [valuation] int
    ("revaluation_interval_s = 5 ", "revaluation_interval_s"),  # [valuation] float
    ("band_equity_qty_e2 = 50_000", "band_equity_qty_e2"),  # [rehedge]
    ("tolerance_qty_e2 = 0", "tolerance_qty_e2"),  # [recon]
    ("theta_days_per_year = 365", "theta_days_per_year"),  # [greeks]
    ("max_rel_std_error = 0.02", "max_rel_std_error"),  # [greeks]
    ("n_steps_per_year = 252", "n_steps_per_year"),  # [models] ModelDefaults
    ("s0 = 187.50", "s0"),  # [models.AAPL] HestonParams
]


@pytest.mark.parametrize(("old", "key"), _NUMERIC_EDITS)
@pytest.mark.parametrize("bad", ["true", "false", '"1000"', '"365"'])
def test_numeric_fields_reject_bool_and_str(tmp_path: Path, old: str, key: str, bad: str) -> None:
    with pytest.raises(ValueError, match="expected a number"):
        load_config(_variant(tmp_path, old, f"{key} = {bad}"))


def test_a_toml_int_is_still_accepted_for_a_float_field(tmp_path: Path) -> None:
    cfg = load_config(_variant(tmp_path, "max_rel_std_error = 0.02", "max_rel_std_error = 1"))
    assert cfg.greeks.max_rel_std_error == 1.0
    cfg = load_config(_variant(tmp_path, "s0 = 187.50", "s0 = 187"))
    assert cfg.models.params["AAPL"].s0 == 187.0
    assert cfg.valuation.revaluation_interval_s == 5.0  # int in exo.toml, float in the model


def test_strict_booleans_stay_strict_and_other_models_reject_bool_and_str() -> None:
    from exo.greeks.bump import BumpConfig
    from exo.models.params import EngineConfig, HestonParams

    good = real_config().models.params["AAPL"].model_dump()
    for bad in (True, "1.0"):
        with pytest.raises(ValueError, match="expected a number"):
            HestonParams.model_validate({**good, "s0": bad})
        with pytest.raises(ValueError, match="expected a number"):
            BumpConfig.model_validate({**real_config().greeks.model_dump(), "bump_vol_abs": bad})
        with pytest.raises(ValueError, match="expected a number"):
            EngineConfig(scheme="qe", n_steps=bad, n_paths=10, expiry=1.0)  # type: ignore[arg-type]
    assert HestonParams.model_validate({**good, "s0": 100}).s0 == 100.0


# --- refdata: malformed universe.json is a ValueError, not a KeyError ---------------------------


@pytest.mark.parametrize(
    "doc",
    ["[]", "{}", '{"books": [], "instruments": 3}', '{"books": [1], "instruments": []}',
     '{"books": [], "instruments": ["x"]}'],
)  # fmt: skip
def test_malformed_universe_raises_value_error(tmp_path: Path, doc: str) -> None:
    path = tmp_path / "u.json"
    path.write_text(doc)
    with pytest.raises(ValueError, match="universe|array of objects"):
        load_universe(path)
