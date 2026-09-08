"""Tests for exo.provenance: git_sha, params_hash, RunManifest, ValuationMetaFields.

Amendment A3 (2026-09-06): ValuationMetaFields must structurally mirror
protocol/proto/common.proto's ValuationMeta message. This test file is the one place allowed to
import the generated proto for that assertion -- provenance.py itself must not (the proto boundary
stays P2.M4's).
"""

import dataclasses
import math
import subprocess
from pathlib import Path

import pytest

from exo.bus.gen import common_pb2
from exo.provenance import (
    EngineSettings,
    ProvenanceError,
    RunManifest,
    ValuationMetaFields,
    git_sha,
    params_hash,
    to_valuation_meta,
)

# --- params_hash -------------------------------------------------------------------------------


def test_params_hash_invariant_to_dict_insertion_order() -> None:
    a = {"s0": 100.0, "kappa": 1.5, "theta": 0.04}
    b = {"theta": 0.04, "s0": 100.0, "kappa": 1.5}
    assert params_hash(a) == params_hash(b)


def test_params_hash_invariant_to_nested_dict_insertion_order() -> None:
    a = {"AAPL": {"s0": 187.5, "rho": -0.7}, "MSFT": {"s0": 420.0, "rho": -0.5}}
    b = {"MSFT": {"rho": -0.5, "s0": 420.0}, "AAPL": {"rho": -0.7, "s0": 187.5}}
    assert params_hash(a) == params_hash(b)


def test_params_hash_sensitive_to_every_top_level_parameter() -> None:
    base = {"s0": 100.0, "r": 0.02, "q": 0.01, "v0": 0.04, "kappa": 1.5, "theta": 0.04, "xi": 0.6}
    baseline = params_hash(base)
    for key in base:
        mutated = dict(base)
        mutated[key] = mutated[key] + 1.0
        assert params_hash(mutated) != baseline, f"hash did not change when {key} changed"


def test_params_hash_sensitive_to_nested_parameter() -> None:
    base = {"AAPL": {"s0": 187.5, "kappa": 1.5}}
    mutated = {"AAPL": {"s0": 187.5, "kappa": 1.6}}
    assert params_hash(base) != params_hash(mutated)


def test_params_hash_rejects_nan() -> None:
    with pytest.raises(ValueError):
        params_hash({"s0": math.nan})


def test_params_hash_rejects_inf() -> None:
    with pytest.raises(ValueError):
        params_hash({"s0": math.inf})


def test_params_hash_distinguishes_negative_zero_from_zero() -> None:
    assert params_hash({"rho_correction": -0.0}) != params_hash({"rho_correction": 0.0})


def test_params_hash_is_sha256_hexdigest_shape() -> None:
    digest = params_hash({"s0": 100.0})
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_params_hash_rejects_non_str_top_level_key() -> None:
    """P1-1 (security review, MEDIUM, 2026-09-06): the signature already declares
    `Mapping[str, object]`; a prior version silently coerced every key with `str(k)` instead of
    enforcing that contract, so `params_hash({1: "x"})` and `params_hash({"1": "x"})` collided,
    and `params_hash({1: "first", "1": "second"})` silently DROPPED the `1` entry (survivor by
    insertion order). Not reachable from today's call sites (pydantic `model_dump()` and
    `tomllib` both yield str keys), but `params_hash` is documented as generic infrastructure for
    P2.M4's `bus/convert.py`, which could hit it."""
    with pytest.raises(TypeError):
        params_hash({1: "x"})  # type: ignore[dict-item]


def test_params_hash_rejects_non_str_nested_key() -> None:
    with pytest.raises(TypeError):
        params_hash({"AAPL": {1: "x"}})  # type: ignore[dict-item]


def test_params_hash_does_not_silently_collide_int_and_str_keys() -> None:
    """The exact collision the security review demonstrated: without the fix, both of these
    calls raise (a non-str key anywhere is now a loud TypeError), so they cannot silently
    collide or silently drop an entry."""
    with pytest.raises(TypeError):
        params_hash({1: "x"})  # type: ignore[dict-item]
    with pytest.raises(TypeError):
        params_hash({1: "first", "1": "second"})  # type: ignore[dict-item]


# --- RunManifest / TOML round-trip --------------------------------------------------------------


def _sample_manifest() -> RunManifest:
    return RunManifest(
        schema_version=1,
        run_id="2026-09-05T12-00-00Z-a1b2c3d4",
        created_ns=1_757_068_800_000_000_000,
        git_sha="a" * 40,
        model_id="heston-qe-v1",
        params_hash="b" * 64,
        seed=20260905,
        n_paths=200_000,
        engine=EngineSettings(scheme="qe", n_steps=252, antithetic=True),
        params={
            "AAPL": {
                "s0": 187.5,
                "r": 0.0425,
                "q": 0.0050,
                "v0": 0.04,
                "kappa": 1.5,
                "theta": 0.04,
                "xi": 0.6,
                "rho": -0.7,
            }
        },
    )


def test_manifest_toml_round_trip(tmp_path: Path) -> None:
    manifest = _sample_manifest()
    path = tmp_path / "run.toml"
    manifest.write(path)
    loaded = RunManifest.read(path)
    assert loaded == manifest


def test_manifest_toml_round_trip_is_actual_toml(tmp_path: Path) -> None:
    import tomllib

    manifest = _sample_manifest()
    path = tmp_path / "run.toml"
    manifest.write(path)
    doc = tomllib.loads(path.read_text())
    assert doc["seed"] == 20260905
    assert doc["engine"]["scheme"] == "qe"
    assert doc["params"]["AAPL"]["s0"] == 187.5


def test_manifest_rejects_seed_too_wide_for_uint64() -> None:
    with pytest.raises(ValueError):
        RunManifest(
            schema_version=1,
            run_id="r",
            created_ns=1,
            git_sha="a" * 40,
            model_id="m",
            params_hash="h",
            seed=2**64,
            n_paths=1,
            engine=EngineSettings(scheme="qe", n_steps=1, antithetic=False),
            params={},
        )


def test_manifest_rejects_n_paths_too_wide_for_uint32() -> None:
    with pytest.raises(ValueError):
        RunManifest(
            schema_version=1,
            run_id="r",
            created_ns=1,
            git_sha="a" * 40,
            model_id="m",
            params_hash="h",
            seed=1,
            n_paths=2**32,
            engine=EngineSettings(scheme="qe", n_steps=1, antithetic=False),
            params={},
        )


def test_manifest_accepts_max_valid_widths() -> None:
    manifest = RunManifest(
        schema_version=1,
        run_id="r",
        created_ns=1,
        git_sha="a" * 40,
        model_id="m",
        params_hash="h",
        seed=2**64 - 1,
        n_paths=2**32 - 1,
        engine=EngineSettings(scheme="qe", n_steps=1, antithetic=False),
        params={},
    )
    assert manifest.seed == 2**64 - 1
    assert manifest.n_paths == 2**32 - 1


def test_manifest_read_rejects_unknown_schema_version(tmp_path: Path) -> None:
    """P2 (code review 2026-09-06): `RunManifest.read` previously never checked
    `schema_version`, so a future v2 manifest would be silently read as v1 and mis-fielded."""
    manifest = _sample_manifest()
    path = tmp_path / "run.toml"
    manifest.write(path)
    raw = path.read_text().replace("schema_version = 1", "schema_version = 2")
    path.write_text(raw)
    with pytest.raises(ValueError, match="schema_version"):
        RunManifest.read(path)


# --- to_valuation_meta / A3 proto structural parity ---------------------------------------------


def test_to_valuation_meta_carries_the_reproducibility_triple() -> None:
    manifest = _sample_manifest()
    meta = to_valuation_meta(manifest)
    assert meta == ValuationMetaFields(
        model_id="heston-qe-v1",
        params_hash="b" * 64,
        seed=20260905,
        n_paths=200_000,
        git_sha="a" * 40,
    )


def test_valuation_meta_fields_matches_proto_field_names_and_order() -> None:
    expected = tuple(common_pb2.ValuationMeta.DESCRIPTOR.fields_by_name.keys())
    actual = tuple(f.name for f in dataclasses.fields(ValuationMetaFields))
    assert actual == expected


# --- git_sha -------------------------------------------------------------------------------------


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=path, check=True)


def test_git_sha_env_override_is_returned_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXO_GIT_SHA", "deadbeef" * 5)
    assert git_sha() == "deadbeef" * 5


def test_git_sha_shape_on_clean_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("EXO_GIT_SHA", raising=False)
    _init_git_repo(tmp_path)
    sha = git_sha(repo=tmp_path)
    expected = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert sha == expected
    assert len(sha) == 40
    assert not sha.endswith("-dirty")


def test_git_sha_dirty_suffix_on_modified_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("EXO_GIT_SHA", raising=False)
    _init_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("changed\n")
    sha = git_sha(repo=tmp_path)
    assert sha.endswith("-dirty")
    assert len(sha) == 40 + len("-dirty")


def test_git_sha_raises_provenance_error_outside_a_git_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("EXO_GIT_SHA", raising=False)
    with pytest.raises(ProvenanceError):
        git_sha(repo=tmp_path)  # tmp_path is not inside a git repository


def test_git_sha_ignores_cwd_and_uses_repo_param(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P1-4 (code review 2026-09-06): git_sha() previously ran `git` with no `cwd=`, so it
    silently read whatever repository the CALLER's CWD happened to be in -- run the pricer from
    elsewhere and you stamp a DIFFERENT repo's sha onto a number, or get a spurious
    ProvenanceError from a perfectly good checkout. Prove the CWD is now irrelevant: chdir into
    a directory that is NOT a git repo, and pass a real repo via `repo=` -- the default (no
    `repo=`) anchors to the exo package's own directory, not the caller's CWD."""
    monkeypatch.delenv("EXO_GIT_SHA", raising=False)
    _init_git_repo(tmp_path)
    not_a_repo = tmp_path.parent / "not-a-repo"
    not_a_repo.mkdir()
    monkeypatch.chdir(not_a_repo)
    sha = git_sha(repo=tmp_path)
    assert len(sha) == 40
    assert not sha.endswith("-dirty")


def test_git_sha_default_repo_is_this_package_not_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The no-argument default must resolve against the exo package's own directory (which is
    inside the real hedging-desk repo), not the process CWD -- chdir somewhere that is not a git
    repo at all and confirm git_sha() with NO repo= still succeeds."""
    monkeypatch.delenv("EXO_GIT_SHA", raising=False)
    monkeypatch.chdir(tmp_path)  # tmp_path is not inside a git repository
    sha = git_sha()
    assert len(sha) == 40 or len(sha) == 40 + len("-dirty")
