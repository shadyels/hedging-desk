"""Fast CI smoke test for `exo.studies.scheme_convergence`.

Runs the study's pure functions at TINY size (few paths, few steps, one batch) so this test stays
fast (CI runs it); asserts the ranking function returns one of the two schemes. Also regression-
tests the two defects the orchestrator found in a prior revision: (1) `simulate()` must be called
once per batch, shared across every strike, not once per (batch, strike); (2) batch sizing must
respect a peak-memory ceiling that varies with `n_steps`, not a fixed path count. The expensive
real sweep that produces `docs/studies/p2m1-scheme-convergence.md` is a manual
`python -m exo.studies.scheme_convergence` command, run by the orchestrator -- not exercised here.
"""

from __future__ import annotations

import math

import pytest

from exo.models.heston import PathBundle
from exo.models.params import EngineConfig, HestonParams
from exo.models.rng import RandomSource
from exo.provenance import EngineSettings, RunManifest
from exo.studies import scheme_convergence
from exo.studies.scheme_convergence import (
    FELLER_VIOLATING,
    SweepCell,
    measure_antithetic_reduction,
    price_batched_multi_strike,
    rank_schemes,
    render_report,
    resolve_batch_plan,
    run_study,
    run_sweep,
)


def test_run_sweep_tiny_produces_one_row_per_cell() -> None:
    rows = run_sweep(
        paths_per_cell=200,
        n_paths_per_batch_override=200,
        base_seed=1,
        schemes=("qe", "euler-ft"),
        n_steps_grid=(4, 12),
        strikes=(100.0,),
        expiries=(1.0,),
        verbose=False,
    )
    # 2 schemes x 2 n_steps x 2 param sets (feller_satisfying/violating) x 1 strike x 1 expiry
    assert len(rows) == 8
    assert all(isinstance(row, SweepCell) for row in rows)
    assert all(row.se > 0.0 for row in rows)
    assert all(row.n_batches == 1 for row in rows)


def test_rank_schemes_returns_one_of_the_two_schemes() -> None:
    rows = run_sweep(
        paths_per_cell=200,
        n_paths_per_batch_override=200,
        base_seed=1,
        schemes=("qe", "euler-ft"),
        n_steps_grid=(4, 12),
        strikes=(100.0,),
        expiries=(1.0,),
        verbose=False,
    )
    chosen = rank_schemes(rows)
    assert chosen in ("qe", "euler-ft")


def test_rank_schemes_picks_coarser_passing_step_count() -> None:
    """Hand-built rows: 'qe' passes (|bias| < 3*se, both param sets) already at n_steps=4;
    'euler-ft' only passes at n_steps=12. rank_schemes must prefer the coarser-passing scheme,
    even though euler-ft's failing n_steps=4 rows are individually faster (wall_time_s)."""
    rows = [
        SweepCell("qe", 4, "feller_satisfying", 100.0, 1.0, 10.0, 1.0, 0.1, 0.1, 1.0, 200, 1),
        SweepCell("qe", 4, "feller_violating", 100.0, 1.0, 10.0, 1.0, -0.2, -0.2, 1.0, 200, 1),
        SweepCell("euler-ft", 4, "feller_satisfying", 100.0, 1.0, 10.0, 1.0, 5.0, 5.0, 0.1, 200, 1),
        SweepCell("euler-ft", 4, "feller_violating", 100.0, 1.0, 10.0, 1.0, 5.0, 5.0, 0.1, 200, 1),
        SweepCell(
            "euler-ft", 12, "feller_satisfying", 100.0, 1.0, 10.0, 1.0, 0.1, 0.1, 0.2, 200, 1
        ),
        SweepCell("euler-ft", 12, "feller_violating", 100.0, 1.0, 10.0, 1.0, 0.1, 0.1, 0.2, 200, 1),
    ]
    assert rank_schemes(rows) == "qe"


def test_measure_antithetic_reduction_returns_a_finite_positive_ratio() -> None:
    ratio = measure_antithetic_reduction(
        FELLER_VIOLATING, "qe", n_steps=4, expiry=1.0, strike=100.0, n_paths=200, seed=1
    )
    assert math.isfinite(ratio)
    assert ratio > 0.0


def test_render_report_states_illustrative_uncalibrated_up_front() -> None:
    manifest = RunManifest(
        schema_version=1,
        run_id="test-run",
        created_ns=0,
        git_sha="deadbeef" * 5,
        model_id="heston-scheme-convergence-v1",
        params_hash="abc123",
        seed=1,
        n_paths=400,
        engine=EngineSettings(scheme="qe", n_steps=4, antithetic=True),
        params={},
    )
    rows = run_sweep(
        paths_per_cell=200,
        n_paths_per_batch_override=200,
        base_seed=1,
        schemes=("qe", "euler-ft"),
        n_steps_grid=(4,),
        strikes=(100.0,),
        expiries=(1.0,),
        verbose=False,
    )
    report = render_report(rows, "qe", {"qe": 1.1, "euler-ft": 0.9}, manifest)
    head = report[:400].lower()
    assert "illustrative" in head
    assert "uncalibrated" in head


def test_run_study_end_to_end_at_tiny_size() -> None:
    result = run_study(
        paths_per_cell=200,
        n_paths_per_batch_override=200,
        base_seed=1,
        schemes=("qe", "euler-ft"),
        n_steps_grid=(4, 12),
        strikes=(100.0,),
        expiries=(1.0,),
        verbose=False,
    )
    assert result.chosen_scheme in ("qe", "euler-ft")
    assert set(result.antithetic_reduction) == {"qe", "euler-ft"}
    assert result.manifest.seed == 1


# --- DEFECT regression tests (orchestrator-reported, fixed in this revision) ---------------------


def test_resolve_batch_plan_respects_memory_ceiling_at_fine_step_count() -> None:
    """DEFECT 2 regression: at n_steps=1008 a FIXED 200k-path batch needs
    200_000 * 1009 * 2 * 8 bytes ~= 3.23 GB, 4x models/heston.py's ~810 MB ceiling.
    resolve_batch_plan must derive a batch small enough to respect max_batch_bytes."""
    n_paths_per_batch, n_batches = resolve_batch_plan(
        n_steps=1008, paths_per_cell=3_200_000, max_batch_bytes=800_000_000
    )
    footprint = n_paths_per_batch * (1008 + 1) * 2 * 8
    assert footprint <= 800_000_000
    assert n_paths_per_batch % 2 == 0
    assert n_paths_per_batch >= 2
    # equal-size batches, total rounded UP from paths_per_cell (no ragged final batch)
    assert n_paths_per_batch * n_batches >= 3_200_000


def test_resolve_batch_plan_uses_one_batch_when_paths_per_cell_fits_in_memory() -> None:
    """At a coarse n_steps the whole paths_per_cell fits under max_batch_bytes in a single
    batch -- no need to split it."""
    n_paths_per_batch, n_batches = resolve_batch_plan(
        n_steps=4, paths_per_cell=3_200_000, max_batch_bytes=800_000_000
    )
    assert n_batches == 1
    assert n_paths_per_batch == 3_200_000


def test_resolve_batch_plan_scales_down_as_n_steps_grows() -> None:
    """The whole point of DEFECT 2's fix: n_paths_per_batch must shrink as n_steps grows, for a
    fixed max_batch_bytes -- a FIXED batch size was exactly the bug."""
    coarse, _ = resolve_batch_plan(n_steps=4, paths_per_cell=3_200_000, max_batch_bytes=800_000_000)
    fine, _ = resolve_batch_plan(
        n_steps=1008, paths_per_cell=3_200_000, max_batch_bytes=800_000_000
    )
    assert fine < coarse


def test_resolve_batch_plan_override_is_floored_even_and_warns_if_over_ceiling(
    capsys: pytest.CaptureFixture[str],
) -> None:
    n_paths_per_batch, n_batches = resolve_batch_plan(
        n_steps=1008,
        paths_per_cell=1000,
        max_batch_bytes=1,  # deliberately tiny, so any positive override "exceeds" it
        n_paths_per_batch_override=333,
    )
    assert n_paths_per_batch == 332  # floored to even
    assert n_batches == math.ceil(1000 / 332)
    assert "WARNING" in capsys.readouterr().out


def test_price_batched_multi_strike_simulates_once_per_batch_not_once_per_strike(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DEFECT 1 regression: simulate() must be called once per batch, shared across every
    strike -- not once per (batch, strike), which would triple (or more) the compute for
    nothing since the simulation itself does not depend on strike."""
    call_count = 0
    real_simulate = scheme_convergence.simulate

    def counting_simulate(
        params: HestonParams, engine: EngineConfig, rng: RandomSource
    ) -> PathBundle:
        nonlocal call_count
        call_count += 1
        return real_simulate(params, engine, rng)

    monkeypatch.setattr(scheme_convergence, "simulate", counting_simulate)

    n_batches = 3
    results, wall_times = price_batched_multi_strike(
        FELLER_VIOLATING,
        "qe",
        n_steps=4,
        expiry=1.0,
        strikes=(90.0, 100.0, 110.0),
        n_paths_per_batch=200,
        n_batches=n_batches,
        base_seed=1,
    )
    assert call_count == n_batches  # NOT n_batches * len(strikes)
    assert set(results) == {90.0, 100.0, 110.0}
    assert set(wall_times) == {90.0, 100.0, 110.0}
    assert all(result.pv > 0.0 for result in results.values())
