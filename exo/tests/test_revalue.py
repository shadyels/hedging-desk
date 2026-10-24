"""`portfolio/revalue.py`: the pure, picklable per-book revaluation job."""

from __future__ import annotations

import pickle
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest
from _book_fixtures import book_doc, real_config, real_universe, storyline_product_doc

from exo.bus.convert import valuation_snapshot_pb
from exo.portfolio.book import parse_book
from exo.portfolio.revalue import BookJob, BookResult, model_id, revalue_book
from exo.provenance import RunManifest, params_hash, to_valuation_meta

CFG = real_config()
UNI = real_universe(CFG)
_WARRANT = {
    "product_id": "EXO-SP-W-0001", "type": "warrant_put", "desk_quantity": -50.0,
    "underlying": "AAPL", "strike": 187.5, "expiry": 1.0, "ratio": 1.0, "exercise": "european",
}  # fmt: skip


def _job(**over: object) -> BookJob:
    (book,) = parse_book(
        book_doc(storyline_product_doc(), _WARRANT, seed=99, n_paths=2000), CFG, UNI
    )
    fields: dict[str, object] = {
        "book": book,
        "marks": {"AAPL": 187.51},
        "params": CFG.models.params,
        "scheme": CFG.models.defaults.scheme,
        "n_steps_per_year": CFG.models.defaults.n_steps_per_year,
        "bumps": CFG.greeks,
        "git_sha": "a" * 40,
        "created_ns": 1_792_000_000_123_456_789,
    }
    fields.update(over)
    return BookJob(**fields)  # type: ignore[arg-type]


def test_revalue_book_returns_lines_targets_and_manifest() -> None:
    result = revalue_book(_job())
    assert [ln.product_id for ln in result.lines] == ["EXO-SP-AC-0001", "EXO-SP-W-0001"]
    assert set(result.targets) == {1001}
    assert result.targets[1001] % 100 == 0
    m = result.manifest
    assert (m.schema_version, m.seed, m.n_paths, m.git_sha) == (3, 99, 2000, "a" * 40)
    assert m.model_id == model_id("qe") == "heston-qe-pseudo-antithetic-v1"
    assert (m.engine.n_steps, m.engine.n_steps_per_year, m.engine.antithetic) == (0, 252, True)
    assert m.engine.sampler == "pseudo"
    assert set(m.params) == {"AAPL"} and m.params["AAPL"]["s0"] == 187.51  # s0 = the mark
    assert m.params["AAPL"]["kappa"] == CFG.models.params["AAPL"].kappa
    assert m.params_hash == params_hash(m.params)
    assert m.run_id.startswith("2026-") and "EXO-SP" in m.run_id and m.run_id.endswith("123456789")


def test_manifest_round_trips_and_feeds_the_snapshot_meta(tmp_path: Path) -> None:
    result = revalue_book(_job())
    path = tmp_path / "m.toml"
    result.manifest.write(path)
    assert RunManifest.read(path) == result.manifest
    meta = to_valuation_meta(result.manifest)
    pb = valuation_snapshot_pb(book_id=3, as_of_ns=1, sent_ns=2, valuation=meta, lines=result.lines)
    assert (pb.valuation.seed, pb.valuation.n_paths, pb.valuation.git_sha) == (99, 2000, "a" * 40)
    assert pb.valuation.params_hash == result.manifest.params_hash
    assert len(pb.lines) == 2


def test_desk_signs_flow_through_to_the_lines() -> None:
    result = revalue_book(_job())
    ac, put = result.lines
    # short a (positive-delta) note and short a put: the desk's delta is negative on the note ...
    assert ac.delta.value < 0
    # ... and POSITIVE on the short put (a put has negative delta; desk is short it)
    assert put.delta.value > 0


def test_revalue_book_is_deterministic() -> None:
    a, b = revalue_book(_job()), revalue_book(_job())
    assert a.lines == b.lines and a.targets == b.targets and a.manifest == b.manifest


def test_a_different_mark_changes_params_hash_and_values() -> None:
    a, b = revalue_book(_job()), revalue_book(_job(marks={"AAPL": 176.0}))
    assert a.manifest.params_hash != b.manifest.params_hash
    assert a.lines[0].delta != b.lines[0].delta


def test_missing_mark_raises() -> None:
    with pytest.raises(ValueError, match="no mark for \\['AAPL'\\]"):
        revalue_book(_job(marks={"MSFT": 420.0}))


def test_job_and_result_are_picklable_and_run_in_a_process_pool() -> None:
    job = _job()
    assert pickle.loads(pickle.dumps(job)) == job
    with ProcessPoolExecutor(max_workers=1) as pool:
        remote: BookResult = pool.submit(revalue_book, job).result(timeout=120)
    assert remote == revalue_book(job)
