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
    monkeypatch.chdir(tmp_path)
    sha = git_sha()
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
    monkeypatch.chdir(tmp_path)
    sha = git_sha()
    assert sha.endswith("-dirty")
    assert len(sha) == 40 + len("-dirty")


def test_git_sha_raises_provenance_error_outside_a_git_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("EXO_GIT_SHA", raising=False)
    monkeypatch.chdir(tmp_path)  # tmp_path is not inside a git repository
    with pytest.raises(ProvenanceError):
        git_sha()
