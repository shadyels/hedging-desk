"""Fast CI smoke test for `exo.studies.qmc_variance_reduction` (T8).

Runs a tiny 2-cell sweep so this test stays fast (CI runs it); the real sweep is a manual
`python -m exo.studies.qmc_variance_reduction` invocation (T9), not exercised here. Also
regression-tests the two degenerate cases `rank_configs` must raise on rather than fall back to
a wall-time or best-effort pick (module docstring).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exo.models.heston import simulate
from exo.models.params import EngineConfig
from exo.models.rng import PseudoRandomSource
from exo.provenance import RunManifest
from exo.studies.qmc_variance_reduction import (
    _AUTOCALL_OBSERVATIONS,
    _PARAMS,
    DEFAULT_N_STEPS,
    Cell,
    ConfigResult,
    InconclusiveVarianceReductionError,
    StudyResult,
    _autocallable,
    _measure_autocall_liveness,
    rank_configs,
    render_report,
    run_study,
)

_SMOKE_CELLS = (
    Cell(product="vanilla", underlying="AAPL", scheme="qe"),
    Cell(product="barrier", underlying="AAPL", scheme="qe"),
)


def _tiny_study() -> StudyResult:
    return run_study(
        repetitions=4,
        replicates=8,
        paths_per_replicate=256,
        n_steps=8,
        base_seed=1,
        cells=_SMOKE_CELLS,
        verbose=False,
    )


def test_run_study_tiny_produces_one_row_per_cell_config() -> None:
    result = _tiny_study()
    assert len(result.cell_reports) == 2

    vanilla_report, barrier_report = result.cell_reports
    assert vanilla_report.cell.product == "vanilla"
    assert {c.config_id for c in vanilla_report.configs} == {1, 2, 3, 4}
    assert barrier_report.cell.product == "barrier"
    assert {c.config_id for c in barrier_report.configs} == {1, 2, 3, 4, 5, 6}

    for cr in result.cell_reports:
        for cfg in cr.configs:
            assert cfg.rmse >= 0.0
            assert cfg.mean_formula_se >= 0.0
            assert cfg.repetitions == 4


def test_run_study_tiny_ranks_or_preserves_evidence_per_cell() -> None:
    result = _tiny_study()
    for cr in result.cell_reports:
        # Exactly one of (best, error) is set -- never both, never neither.
        assert (cr.best is not None) != (cr.error is not None)
        if cr.best is not None:
            assert cr.best.config_id != 1


def test_run_study_tiny_manifest_round_trips(tmp_path: Path) -> None:
    result = _tiny_study()
    manifest_path = tmp_path / "manifest.toml"
    result.manifest.write(manifest_path)
    read_back = RunManifest.read(manifest_path)
    assert read_back.seed == result.manifest.seed
    assert read_back.git_sha == result.manifest.git_sha
    assert read_back.params_hash == result.manifest.params_hash
    assert read_back.engine.sampler == "sobol"
    assert read_back.engine.n_replicates == 8
    assert read_back.engine.bridge_streams == ("spot",)


def test_render_report_tiny_produces_nonempty_markdown() -> None:
    result = _tiny_study()
    report = render_report(result.cell_reports, result.manifest, repetitions=4)
    assert report.startswith("# P2.M1 Slice 3")
    assert "## Cell results" in report
    assert "## Peak memory" in report
    assert "## SE calibration" in report


def _config_result(config_id: int, ratio: float, *, rel_precision: float = 0.13) -> ConfigResult:
    return ConfigResult(
        cell_label="synthetic/AAPL/qe",
        config_id=config_id,
        config_label=f"config-{config_id}",
        rmse=1.0 / ratio if ratio > 0 else 1.0,
        mean_formula_se=1.0,
        calibration_factor=1.0,
        ratio_to_denominator=ratio,
        ratio_rel_precision=rel_precision,
        repetitions=32,
    )


def test_rank_configs_raises_when_nothing_beats_the_denominator() -> None:
    rows = [
        _config_result(1, 1.0),
        _config_result(2, 0.95),
        _config_result(3, 0.80),
        _config_result(4, 0.60),
    ]
    with pytest.raises(InconclusiveVarianceReductionError, match="no configuration improves"):
        rank_configs(rows)


def test_rank_configs_raises_when_every_configuration_ties() -> None:
    # Every non-denominator ratio sits well inside the others' combined precision band.
    rows = [
        _config_result(1, 1.0),
        _config_result(2, 4.00, rel_precision=0.13),
        _config_result(3, 4.02, rel_precision=0.13),
        _config_result(4, 3.98, rel_precision=0.13),
    ]
    with pytest.raises(InconclusiveVarianceReductionError, match="every configuration ties"):
        rank_configs(rows)


def test_rank_configs_picks_the_highest_ratio_when_distinguishable() -> None:
    rows = [
        _config_result(1, 1.0),
        _config_result(2, 1.30, rel_precision=0.13),
        _config_result(3, 4.20, rel_precision=0.13),
        _config_result(4, 8.50, rel_precision=0.13),
    ]
    best = rank_configs(rows)
    assert best.config_id == 4


def test_autocallable_observations_align_with_production_grid() -> None:
    """Coordinator correction: `_autocallable`'s observation schedule must land EXACTLY on the
    DEFAULT_N_STEPS simulation grid (`observation_indices` raises `ScheduleAlignmentError`
    otherwise) -- pinned down explicitly so a later `DEFAULT_N_STEPS` change fails loudly here
    instead of silently mis-scheduling every autocallable cell."""
    params = _PARAMS["AAPL"]
    payoff = _autocallable("AAPL", params)
    assert payoff.observations == _AUTOCALL_OBSERVATIONS
    engine = EngineConfig(
        scheme="qe", n_steps=DEFAULT_N_STEPS, n_paths=2, expiry=1.0, antithetic=True
    )
    bundle = simulate(params, engine, PseudoRandomSource(seed=1))
    payoff.cashflows(bundle)  # raises ScheduleAlignmentError if misaligned -- must not raise


@pytest.mark.parametrize("underlying", ["AAPL", "MSFT"])
def test_autocallable_is_genuinely_live_not_degenerate(underlying: str) -> None:
    """Coordinator correction: G3b's own fixture is DELIBERATELY degenerate (never calls, never
    earns a coupon) -- `_autocallable` must not have silently reproduced that. At the study's
    real parameters, a real fraction of paths must redeem early AND a real fraction must never
    redeem, at both underlyings -- 0% or 100% on either side would mean the fixture is still
    degenerate, just in a different direction."""
    liveness = _measure_autocall_liveness(
        underlying, _PARAMS[underlying], n_steps=DEFAULT_N_STEPS, n_paths=2048, seed=7
    )
    total_redeemed = sum(liveness.redeem_fraction)
    assert 0.0 < total_redeemed < 1.0, f"redeemed fraction {total_redeemed} looks degenerate"
    assert 0.0 < liveness.never_redeemed_fraction < 1.0, (
        f"never-redeemed fraction {liveness.never_redeemed_fraction} looks degenerate"
    )
    assert any(f > 0.0 for f in liveness.coupon_fraction), "no path ever earned a coupon"
