"""Fast CI smoke test for `exo.studies.scheme_convergence`.

Runs the study's pure functions at TINY size (few paths, few steps, one batch) so this test stays
fast (CI runs it); asserts the ranking function returns one of the two schemes. Also regression-
tests the defects found across two review rounds (orchestrator's manual review, then
code-reviewer + security-engineer): (1) `simulate()` must be called once per batch, shared across
every strike, not once per (batch, strike); (2) batch sizing must respect a peak-memory ceiling
that varies with `n_steps`, accounting for ALL FOUR allocated arrays, not two; (3) `rank_schemes`
must reject an imprecise "pass"; (4) the QE inadmissibility fallback fraction must be observable.
The expensive real sweep that produces `docs/studies/p2m1-scheme-convergence.md` is a manual
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
    se_tol_for_paths_per_cell,
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
    chosen = rank_schemes(rows, se_tol=se_tol_for_paths_per_cell(200))
    assert chosen in ("qe", "euler-ft")


def test_rank_schemes_picks_coarser_passing_step_count() -> None:
    """Hand-built rows: 'qe' passes (|bias| < 3*se AND se < se_tol=2.0, both param sets) already
    at n_steps=4; 'euler-ft' only passes at n_steps=12. rank_schemes must prefer the
    coarser-passing scheme, even though euler-ft's failing n_steps=4 rows are individually
    faster (wall_time_s)."""
    rows = [
        SweepCell("qe", 4, "feller_satisfying", 100.0, 1.0, 10.0, 1.0, 0.1, 0.1, 1.0, 200, 1, None),
        SweepCell(
            "qe", 4, "feller_violating", 100.0, 1.0, 10.0, 1.0, -0.2, -0.2, 1.0, 200, 1, None
        ),
        SweepCell(
            "euler-ft", 4, "feller_satisfying", 100.0, 1.0, 10.0, 1.0, 5.0, 5.0, 0.1, 200, 1, None
        ),
        SweepCell(
            "euler-ft", 4, "feller_violating", 100.0, 1.0, 10.0, 1.0, 5.0, 5.0, 0.1, 200, 1, None
        ),
        SweepCell(
            "euler-ft",
            12,
            "feller_satisfying",
            100.0,
            1.0,
            10.0,
            1.0,
            0.1,
            0.1,
            0.2,
            200,
            1,
            None,
        ),
        SweepCell(
            "euler-ft",
            12,
            "feller_violating",
            100.0,
            1.0,
            10.0,
            1.0,
            0.1,
            0.1,
            0.2,
            200,
            1,
            None,
        ),
    ]
    assert rank_schemes(rows, se_tol=2.0) == "qe"


def test_rank_schemes_se_tol_conjunct_disqualifies_an_imprecise_pass() -> None:
    """P0-2 (BLOCKER-adjacent, code review 2026-09-06): `rank_schemes` previously had no
    minimum-precision conjunct -- a scheme with high payoff variance could pass `|bias| < 3*se`
    at a coarser step count BECAUSE it is imprecise, and win the ranking for it.

    Scheme "A" passes the bias check at n_steps=4 (bias=2.0 < 3*se=3.0) but ONLY because se=1.0
    is huge; se_tol=0.5 correctly disqualifies it there, and it never becomes precise enough to
    pass at any step count in these rows. Scheme "B" passes BOTH conjuncts at n_steps=4
    (bias=0.05 < 3*0.2=0.6, se=0.2 < 0.5).

    WITHOUT the se_tol conjunct, the OLD rule would have called both "A" and "B" passing at
    n_steps=4 and tie-broken on wall time -- A's n_steps=4 rows are deliberately FASTER
    (wall_time=0.05) than B's (wall_time=1.0), so the old rule would pick "A". WITH se_tol, A is
    disqualified everywhere in these rows (key stays +inf) and B wins outright.
    """
    rows = [
        SweepCell("A", 4, "feller_satisfying", 100.0, 1.0, 10.0, 1.0, 2.0, 2.0, 0.05, 200, 1, None),
        SweepCell("A", 4, "feller_violating", 100.0, 1.0, 10.0, 1.0, 2.0, 2.0, 0.05, 200, 1, None),
        SweepCell("A", 12, "feller_satisfying", 100.0, 1.0, 10.0, 1.0, 2.0, 2.0, 2.0, 200, 1, None),
        SweepCell("A", 12, "feller_violating", 100.0, 1.0, 10.0, 1.0, 2.0, 2.0, 2.0, 200, 1, None),
        SweepCell(
            "B", 4, "feller_satisfying", 100.0, 1.0, 10.0, 0.2, 0.05, 0.25, 1.0, 200, 1, None
        ),
        SweepCell("B", 4, "feller_violating", 100.0, 1.0, 10.0, 0.2, 0.05, 0.25, 1.0, 200, 1, None),
    ]
    assert rank_schemes(rows, se_tol=0.5) == "B"


def test_se_tol_for_paths_per_cell_shrinks_as_paths_grow() -> None:
    """se_tol must tighten (shrink) as paths_per_cell grows -- more paths should demand more
    precision to pass, not less."""
    loose = se_tol_for_paths_per_cell(200)
    tight = se_tol_for_paths_per_cell(3_200_000)
    assert tight < loose
    assert tight > 0.0


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
    report = render_report(
        rows, "qe", {"qe": 1.1, "euler-ft": 0.9}, manifest, se_tol=se_tol_for_paths_per_cell(200)
    )
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


# --- DEFECT regression tests (found across two review rounds, fixed in this revision) -----------


def test_resolve_batch_plan_respects_memory_ceiling_at_fine_step_count() -> None:
    """DEFECT 2 / P0-1 regression: at n_steps=1008 a FIXED 200k-path batch needs
    200_000 * 1009 * `_BYTES_PER_PATH_STEP` bytes, ~4x models/heston.py's ~1.62 GB ceiling once
    ALL FOUR allocated arrays are counted (S, v, and the two draw matrices -- P0-1, code review
    2026-09-06 corrected an earlier version of this constant that counted only S and v).
    resolve_batch_plan must derive a batch small enough to respect max_batch_bytes. This asserts
    against `scheme_convergence._BYTES_PER_PATH_STEP`, the module's OWN constant, rather than a
    re-typed literal -- a re-typed literal would re-encode the same wrong model and could never
    catch a regression in the constant itself."""
    n_paths_per_batch, n_batches = resolve_batch_plan(
        n_steps=1008, paths_per_cell=3_200_000, max_batch_bytes=800_000_000
    )
    footprint = n_paths_per_batch * (1008 + 1) * scheme_convergence._BYTES_PER_PATH_STEP
    assert footprint <= 800_000_000
    assert n_paths_per_batch % 2 == 0
    assert n_paths_per_batch >= 2
    # equal-size batches, total rounded UP from paths_per_cell (no ragged final batch)
    assert n_paths_per_batch * n_batches >= 3_200_000


def test_bytes_per_path_step_accounts_for_all_four_allocated_arrays() -> None:
    """P0-1 (code review 2026-09-06): heston.py's simulate() allocates FOUR (n_paths, n_steps[+1])
    float64 arrays up front -- S, v, and two draw matrices (QE: u, z; euler-ft: z_variance,
    z_spot) -- not two. This pins the constant directly so the memory model cannot silently
    regress back to counting only S and v."""
    assert scheme_convergence._BYTES_PER_PATH_STEP == 4 * 8


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


def test_resolve_batch_plan_memory_derived_branch_warns_if_floor_of_2_still_exceeds_budget(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """P2 (code review 2026-09-06): symmetry with the explicit-override branch's warning above --
    the memory-derived branch floors to a minimum of 2 paths, which can itself exceed a
    sufficiently tiny max_batch_bytes. Unreachable at production values, but should warn rather
    than silently proceed, same as the override branch does."""
    n_paths_per_batch, _ = resolve_batch_plan(n_steps=1008, paths_per_cell=1000, max_batch_bytes=1)
    assert n_paths_per_batch == 2
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
    results, wall_times, _qe_fraction = price_batched_multi_strike(
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


def test_price_batched_multi_strike_reports_qe_fallback_fraction() -> None:
    """P0-3 (BLOCKER, code review 2026-09-06): Amendment A1 required a ponytail marker for the
    QE inadmissibility fallback, but `qe_fallback_count` was consumed by NOTHING -- not a
    `SweepCell` field, not in `render_report`. A shortcut whose upgrade trigger cannot be
    measured is untracked debt. price_batched_multi_strike must aggregate the fallback fraction
    across all batches (shared across every strike, since it is a property of the shared bundle,
    not of the payoff) and report None for euler-ft, which has no such diagnostic."""
    _, _, qe_fraction = price_batched_multi_strike(
        FELLER_VIOLATING,
        "qe",
        n_steps=4,
        expiry=1.0,
        strikes=(90.0, 100.0, 110.0),
        n_paths_per_batch=200,
        n_batches=2,
        base_seed=1,
    )
    assert qe_fraction is not None
    assert 0.0 <= qe_fraction <= 1.0

    _, _, euler_fraction = price_batched_multi_strike(
        FELLER_VIOLATING,
        "euler-ft",
        n_steps=4,
        expiry=1.0,
        strikes=(90.0, 100.0, 110.0),
        n_paths_per_batch=200,
        n_batches=2,
        base_seed=1,
    )
    assert euler_fraction is None


def test_run_sweep_populates_qe_fallback_fraction_on_rows() -> None:
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
    qe_rows = [row for row in rows if row.scheme == "qe"]
    euler_rows = [row for row in rows if row.scheme == "euler-ft"]
    assert qe_rows and all(row.qe_fallback_fraction is not None for row in qe_rows)
    assert euler_rows and all(row.qe_fallback_fraction is None for row in euler_rows)
