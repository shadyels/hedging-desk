"""Fast CI smoke test for `exo.studies.scheme_convergence`.

Runs the study's pure functions at TINY size (few paths, few steps, one batch) so this test stays
fast (CI runs it); asserts the ranking function returns one of the two schemes. Also regression-
tests the defects found across three review rounds (orchestrator's manual review, then two
code-reviewer + security-engineer rounds): (1) `simulate()` must be called once per batch, shared
across every strike, not once per (batch, strike); (2) batch sizing must respect a peak-memory
ceiling, MEASURED with `tracemalloc` against the real `simulate()` rather than hand-counted (a
hand re-count got this wrong twice); (3) `rank_schemes` must reject an imprecise "pass" without
ever degenerating into a wall-time race, and must fail loudly (never silently) when the grid
cannot support a decision; (4) the QE inadmissibility fallback fraction must be observable and its
non-zero path exercised. The expensive real sweep that produces
`docs/studies/p2m1-scheme-convergence.md` is a manual `python -m exo.studies.scheme_convergence`
command, run by the orchestrator -- not exercised here.
"""

from __future__ import annotations

import math
import tracemalloc
from pathlib import Path

import pytest

from exo.models.heston import PathBundle
from exo.models.params import EngineConfig, HestonParams
from exo.models.rng import PseudoRandomSource, RandomSource
from exo.provenance import EngineSettings, RunManifest
from exo.studies import scheme_convergence
from exo.studies.scheme_convergence import (
    FELLER_VIOLATING,
    SweepCell,
    convergence_costs,
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
    """SHOULD-2 (third code-review round, 2026-09-06): `paths_per_cell=2000` (this test's
    previous size) measures `se ~= 0.21` with bias/se ratios of 0.14-0.37 for QE at EVERY step
    count -- inside the noise regime, so the ranking passes on a coincidence rather than a real
    discrimination between schemes. `paths_per_cell=20_000` with this grid is independently
    verified (before writing this test) to give a genuine, non-coincidental separation: QE first
    passes at n_steps=4, euler-ft only at n_steps=52 -- while still running in ~1.5s."""
    rows = run_sweep(
        paths_per_cell=20_000,
        n_paths_per_batch_override=20_000,
        base_seed=20260906,
        schemes=("qe", "euler-ft"),
        n_steps_grid=(4, 12, 52, 104),
        strikes=(90.0, 100.0, 110.0),
        expiries=(0.5, 2.0),
        verbose=False,
    )
    chosen = rank_schemes(rows)
    assert chosen in ("qe", "euler-ft")


def test_rank_schemes_raises_when_every_scheme_first_passes_at_the_coarsest_step() -> None:
    """SHOULD-2 (third code-review round, 2026-09-06): `shared_se = min(se)` closes INTER-scheme
    gaming (a noisier scheme can't buy passage by being imprecise relative to its peer), but it
    does not stop BOTH schemes being imprecise AT ONCE -- if every scheme first-passes at the
    COARSEST step count swept, the grid never had a chance to discriminate (shared_se exceeds
    the biases being resolved) and ranking would otherwise degenerate into a wall-time race,
    exactly the class of failure this whole round already fixed once, reached from the other
    side. `paths_per_cell=100` with this grid/seed is independently verified (before writing
    this test) to put BOTH schemes' first pass at n_steps=4, the coarsest step swept."""
    rows = run_sweep(
        paths_per_cell=100,
        n_paths_per_batch_override=100,
        base_seed=20260906,
        schemes=("qe", "euler-ft"),
        n_steps_grid=(4, 12, 52, 104),
        strikes=(90.0, 100.0, 110.0),
        expiries=(0.5, 2.0),
        verbose=False,
    )
    with pytest.raises(ValueError, match="no discriminating power"):
        rank_schemes(rows)


def test_rank_schemes_picks_coarser_passing_step_count() -> None:
    """Hand-built rows: 'qe' passes (|bias| < 3*shared_se, both param sets) already at
    n_steps=4; 'euler-ft' only passes at n_steps=12. rank_schemes must prefer the
    coarser-passing scheme, even though euler-ft's failing n_steps=4 rows are individually
    faster (wall_time_s). Both schemes share `se=1.0` at every cell here, so `shared_se` is
    trivially `1.0` throughout -- this test is about step-count preference, not the shared-SE
    mechanism itself (see test_rank_schemes_shared_se_prevents_a_noisier_scheme_from_passing_on_
    its_own_se below for that)."""
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
    assert rank_schemes(rows) == "qe"


def test_rank_schemes_shared_se_prevents_a_noisier_scheme_from_passing_on_its_own_se() -> None:
    """P0-2, SECOND correction (2026-09-06): the first fix used an absolute `se_tol` that could
    never be satisfied by refining `n_steps` when `se` is set by expiry/strike (payoff variance),
    not step count -- this permanently excluded high-variance cells in the real run and caused
    `rank_schemes` to fall through to a wall-time tie-break for BOTH schemes. The corrected rule
    scores each row against `shared_se = min(se across the schemes present at that exact cell)`:
    refining `n_steps` still helps because it shrinks the discretization BIAS (not `se`, which is
    set by path count and payoff variance and does not trend with `n_steps` -- see
    `_shared_se_per_cell`'s docstring), but a scheme cannot borrow its OWN inflated `se` to
    excuse a real bias.

    Scheme "A" has bias=2.0 and se=1.0 at EVERY step count here (never actually converges):
    under the OLD (pre-code-review) rule, `|bias| < 3*se` (2.0 < 3.0) would have called A
    "passing" already at n_steps=4. Scheme "B" is precise (se=0.1) throughout and only becomes
    accurate at n_steps=12. `shared_se` at every cell here is `min(1.0, 0.1) = 0.1` (B's), so A's
    bias=2.0 fails `2.0 < 3*0.1 = 0.3` at every step count -- A never passes -- and B passes at
    n_steps=12."""
    rows = [
        SweepCell("A", 4, "feller_satisfying", 100.0, 1.0, 10.0, 1.0, 2.0, 2.0, 1.0, 200, 1, None),
        SweepCell("A", 4, "feller_violating", 100.0, 1.0, 10.0, 1.0, 2.0, 2.0, 1.0, 200, 1, None),
        SweepCell("A", 12, "feller_satisfying", 100.0, 1.0, 10.0, 1.0, 2.0, 2.0, 1.0, 200, 1, None),
        SweepCell("A", 12, "feller_violating", 100.0, 1.0, 10.0, 1.0, 2.0, 2.0, 1.0, 200, 1, None),
        SweepCell("B", 4, "feller_satisfying", 100.0, 1.0, 10.0, 0.1, 2.0, 20.0, 1.0, 200, 1, None),
        SweepCell("B", 4, "feller_violating", 100.0, 1.0, 10.0, 0.1, 2.0, 20.0, 1.0, 200, 1, None),
        SweepCell(
            "B", 12, "feller_satisfying", 100.0, 1.0, 10.0, 0.1, 0.05, 0.5, 0.5, 200, 1, None
        ),
        SweepCell("B", 12, "feller_violating", 100.0, 1.0, 10.0, 0.1, 0.05, 0.5, 0.5, 200, 1, None),
    ]
    assert rank_schemes(rows) == "B"


def test_rank_schemes_raises_when_no_scheme_converges_rather_than_wall_time_fallback() -> None:
    """P0-2, SECOND fix (2026-09-06): a fallback that silently ranks by wall time when no scheme
    passes is a defect in its own right -- it is exactly what let a stopwatch reading masquerade
    as a convergence result in the definitive run (every T=2.0 row failed an unsatisfiable
    absolute se_tol for BOTH schemes, so the old rank_schemes fell through to ranking by total
    wall time and picked euler-ft purely because it is faster per step, not because it
    converged). rank_schemes must raise instead of ever reaching that fallback.

    Scheme "A" is much FASTER (wall_time=0.1) than "B" (wall_time=10.0), but NEITHER scheme's
    bias (5.0) falls inside `3*shared_se` (shared_se=1.0, so the bound is 3.0) at the only step
    count present. A naive wall-time fallback would pick "A"; the fix must raise instead."""
    rows = [
        SweepCell("A", 4, "feller_satisfying", 100.0, 1.0, 10.0, 1.0, 5.0, 5.0, 0.1, 200, 1, None),
        SweepCell("A", 4, "feller_violating", 100.0, 1.0, 10.0, 1.0, 5.0, 5.0, 0.1, 200, 1, None),
        SweepCell("B", 4, "feller_satisfying", 100.0, 1.0, 10.0, 1.0, 5.0, 5.0, 10.0, 200, 1, None),
        SweepCell("B", 4, "feller_violating", 100.0, 1.0, 10.0, 1.0, 5.0, 5.0, 10.0, 200, 1, None),
    ]
    with pytest.raises(ValueError, match="no scheme converged"):
        rank_schemes(rows)


def test_rank_schemes_requires_at_least_two_schemes() -> None:
    rows = [
        SweepCell(
            "qe", 4, "feller_satisfying", 100.0, 1.0, 10.0, 0.1, 0.05, 0.5, 1.0, 200, 1, None
        ),
        SweepCell("qe", 4, "feller_violating", 100.0, 1.0, 10.0, 0.1, 0.05, 0.5, 1.0, 200, 1, None),
    ]
    with pytest.raises(ValueError, match="at least two schemes"):
        rank_schemes(rows)


def test_shared_se_degeneracy_guard_is_per_cell_not_global() -> None:
    """SHOULD-3 (third code-review round, 2026-09-06): the "at least two schemes" guard above is
    GLOBAL (checked once over all of `rows`), but `_shared_se_per_cell`'s `min` over "whichever
    schemes are present at a cell" silently reverts to a scheme's OWN `se` at any INDIVIDUAL cell
    where only one scheme has a row -- exactly the bound the shared-SE fix replaced. Not
    reachable from `run_sweep`'s full cross product today, but `rank_schemes` is public and its
    docstring guarantee ("a scheme cannot pass by being noisier than its own peer") is stated
    unconditionally, so a caller building a partial `rows` list (or a future producer that
    doesn't fill the full cross product) must not get a silently degraded answer.

    Here BOTH schemes are present overall (passing the global guard), but "euler-ft" has no row
    at all for `(n_steps=4, feller_violating)` -- only "qe" does, with a huge se=1.0 and
    bias=2.0. Under the OLD per-call-only guard, `shared_se` at that lone cell degenerates to
    qe's own se=1.0, and `|2.0| < 3*1.0=3.0` would let qe "pass" at n_steps=4 on nothing but its
    own imprecision -- qe has no other step count in these rows, so a correct implementation
    must NOT let this count as a pass, and (with no other step count present) `rank_schemes` must
    raise rather than crown qe the winner."""
    rows = [
        SweepCell(
            "qe", 4, "feller_satisfying", 100.0, 1.0, 10.0, 0.1, 0.05, 0.5, 1.0, 200, 1, None
        ),
        SweepCell("qe", 4, "feller_violating", 100.0, 1.0, 10.0, 1.0, 2.0, 2.0, 1.0, 200, 1, None),
        SweepCell(
            "euler-ft", 4, "feller_satisfying", 100.0, 1.0, 10.0, 0.1, 0.05, 0.5, 1.0, 200, 1, None
        ),
        # euler-ft has NO row at (n_steps=4, feller_violating) -- only qe does.
    ]
    with pytest.raises(ValueError, match="no scheme converged"):
        rank_schemes(rows)


def test_convergence_costs_applies_the_same_per_cell_degeneracy_guard() -> None:
    """SHOULD-3: `convergence_costs` shares `_first_passing_step` with `rank_schemes`, so the
    per-cell guard must protect it too -- otherwise `render_report` could show a "converged" cost
    row for a scheme that only "passed" via a degenerate single-scheme shared_se."""
    rows = [
        SweepCell(
            "qe", 4, "feller_satisfying", 100.0, 1.0, 10.0, 0.1, 0.05, 0.5, 1.0, 200, 1, None
        ),
        SweepCell("qe", 4, "feller_violating", 100.0, 1.0, 10.0, 1.0, 2.0, 2.0, 1.0, 200, 1, None),
        SweepCell(
            "euler-ft", 4, "feller_satisfying", 100.0, 1.0, 10.0, 0.1, 0.05, 0.5, 1.0, 200, 1, None
        ),
    ]
    costs = {cost.scheme: cost for cost in convergence_costs(rows)}
    assert costs["qe"].first_passing_n_steps is None


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
    """See test_rank_schemes_returns_one_of_the_two_schemes for why `paths_per_cell=20_000`
    (not the noise-regime 2000 this test previously used) is required for a genuine, not
    coincidental, ranking."""
    result = run_study(
        paths_per_cell=20_000,
        n_paths_per_batch_override=20_000,
        base_seed=20260906,
        schemes=("qe", "euler-ft"),
        n_steps_grid=(4, 12, 52, 104),
        strikes=(90.0, 100.0, 110.0),
        expiries=(0.5, 2.0),
        verbose=False,
    )
    assert result.chosen_scheme in ("qe", "euler-ft")
    assert set(result.antithetic_reduction) == {"qe", "euler-ft"}
    assert result.manifest.seed == 20260906


def test_run_study_preserves_rows_when_ranking_is_inconclusive() -> None:
    """SHOULD-7 (third code-review round, 2026-09-06): `chosen = rank_schemes(rows)` used to run
    immediately after `run_sweep`, so on either `InconclusiveRankingError` raise path (no scheme
    converged, or no discriminating power) the simulated rows -- ~50 minutes of compute in the
    real run -- were lost with no artifact. `run_study` must catch the ranking failure and still
    return a `StudyResult` with `chosen_scheme=None` and every row/antithetic-measurement/
    manifest field populated, so `main()` can render and write a report before failing loudly.
    Uses the same deliberately-noisy config verified in
    test_rank_schemes_raises_when_every_scheme_first_passes_at_the_coarsest_step."""
    result = run_study(
        paths_per_cell=100,
        n_paths_per_batch_override=100,
        base_seed=20260906,
        schemes=("qe", "euler-ft"),
        n_steps_grid=(4, 12, 52, 104),
        strikes=(90.0, 100.0, 110.0),
        expiries=(0.5, 2.0),
        verbose=False,
    )
    assert result.chosen_scheme is None
    assert len(result.rows) > 0
    assert set(result.antithetic_reduction) == {"qe", "euler-ft"}
    assert result.manifest.seed == 20260906


def test_render_report_shows_no_scheme_converged_banner_when_chosen_is_none() -> None:
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
    report = render_report(rows, None, {"qe": 1.1, "euler-ft": 0.9}, manifest)
    assert "NO SCHEME CONVERGED" in report
    # the sweep table itself must still be present -- this is the "preserve the evidence" part.
    assert "## Sweep results" in report
    assert all(row.param_set in report for row in rows[:1])


def test_main_writes_the_report_before_failing_when_ranking_is_inconclusive(
    tmp_path: Path,
) -> None:
    """SHOULD-7: the CLI must not lose the sweep silently -- it writes the report (with the "NO
    SCHEME CONVERGED" banner and the raw rows) and THEN fails loudly (non-zero exit), rather
    than either succeeding silently or crashing with nothing written."""
    out_path = tmp_path / "report.md"
    manifest_dir = tmp_path / "manifests"
    argv = [
        "--paths-per-cell",
        "100",
        "--n-paths-per-batch",
        "100",
        "--seed",
        "20260906",
        "--out",
        str(out_path),
        "--manifest-dir",
        str(manifest_dir),
    ]
    with pytest.raises(SystemExit) as exc_info:
        scheme_convergence.main(argv)
    assert exc_info.value.code != 0
    assert out_path.exists()
    written = out_path.read_text()
    assert "NO SCHEME CONVERGED" in written
    assert "## Sweep results" in written


# --- DEFECT regression tests (found across two review rounds, fixed in this revision) -----------


def test_resolve_batch_plan_respects_memory_ceiling_at_fine_step_count() -> None:
    """DEFECT 2 regression: at n_steps=1008 a FIXED 200k-path batch needs
    200_000 * 1009 * `_BYTES_PER_PATH_STEP` bytes, ~4x models/heston.py's ~2.02 GB tracemalloc-
    measured ceiling (see test_bytes_per_path_step_covers_measured_peak_with_tracemalloc).
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


def test_bytes_per_path_step_covers_measured_peak_with_tracemalloc() -> None:
    """BLOCKER, second code-review round (2026-09-06): a hand re-count of simulate()'s
    allocations got `_BYTES_PER_PATH_STEP` wrong TWICE -- first counting 2 arrays (S, v only),
    then 4 (adding the draw matrices but missing that `PseudoRandomSource._draw`'s antithetic
    `np.concatenate([base, mirror])` holds a THIRD array (the concatenated result) live
    alongside `base`/`mirror` at the moment of concatenation, i.e. a 5th full-size array's worth
    of memory at peak, not 4). A regression test that re-counts the source (as the previous two
    versions of this test did) can only ever certify the same wrong model.

    This measures the REAL simulate() with `tracemalloc` instead, for both schemes and both
    antithetic settings (antithetic=True is the only path this study actually runs; False is
    included for completeness/contrast), and asserts the declared ceiling is not exceeded by
    what actually runs -- `_BYTES_PER_PATH_STEP` bytes per (path, n_steps+1 "step-slot"), matching
    `resolve_batch_plan`'s own bytes-per-path formula.

    Size (20_000 paths x 100 steps) is chosen large enough that tracemalloc's own bookkeeping
    overhead is negligible relative to the arrays measured (small sizes, e.g. a few thousand
    paths, showed ratios over 5.5 purely from that overhead when this test was being written --
    that would be a false positive, not evidence of a wrong constant) while staying well under a
    second to run.
    """
    params = HestonParams(
        s0=100.0, r=0.02, q=0.01, v0=0.04, kappa=1.5, theta=0.04, xi=0.6, rho=-0.7
    )
    n_paths, n_steps = 20_000, 100
    bytes_per_path_step = n_paths * (n_steps + 1)  # matches resolve_batch_plan's own formula
    # Interpreter/allocator noise tolerance: measured ratios at this size cluster at ~4.99-5.08
    # across scheme/antithetic combinations (see this test's own docstring and the module
    # constant's comment) -- 0.15 covers that spread without being loose enough to hide a real
    # regression (e.g. back to a ratio of 4).
    tolerance = 0.15

    for scheme in ("qe", "euler-ft"):
        for antithetic in (True, False):
            engine = EngineConfig(
                scheme=scheme, n_steps=n_steps, n_paths=n_paths, expiry=1.0, antithetic=antithetic
            )
            rng = PseudoRandomSource(seed=1, antithetic=antithetic)
            tracemalloc.start()
            try:
                scheme_convergence.simulate(params, engine, rng)
                _current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            measured_ratio = peak / (bytes_per_path_step * 8)
            declared_ratio = scheme_convergence._BYTES_PER_PATH_STEP / 8
            assert declared_ratio >= measured_ratio - tolerance, (
                f"{scheme} antithetic={antithetic}: measured peak/array ratio "
                f"{measured_ratio:.3f} exceeds the declared _BYTES_PER_PATH_STEP "
                f"({declared_ratio:.1f}) by more than the noise tolerance ({tolerance}) -- "
                "the declared ceiling no longer covers what actually runs"
            )
            assert measured_ratio > 3.5, (
                f"{scheme} antithetic={antithetic}: measured ratio {measured_ratio:.3f} is "
                "implausibly small -- this measurement itself looks broken, not the constant"
            )


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
