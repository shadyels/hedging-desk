"""Provenance for a pricing run: git sha, canonical parameter hashing, and the run manifest.

Root CLAUDE.md invariant #7: every published number carries
`(model_id, params_hash, seed, n_paths, git_sha)`. This module is where that reproducibility
triple gets captured. It lives at the top level of `exo/`, not under `models/`, because the
manifest and `git_sha` are consumed by `bus/` and `portfolio/` in P2.M4 too, and `params_hash`
canonicalization is generic -- `models/` stays pure numerics (see the P2.M1 slice 1 spec).

`provenance.py` deliberately does not import `exo.bus.gen`: the Protobuf boundary belongs to
P2.M4. `ValuationMetaFields` below is a plain dataclass whose field names and order are chosen to
mirror `protocol/proto/common.proto`'s `ValuationMeta` message exactly, so that P2.M4's
`bus/convert.py` is a plain field-by-field copy; the structural parity is asserted by a test
(`exo/tests/test_provenance.py`), which is allowed to import the generated proto even though this
module is not.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Self

import tomli_w

# Proto field widths (protocol/proto/common.proto: `uint64 seed`, `uint32 n_paths`). Validated at
# manifest construction (amendment A4) so an overflow surfaces at config-load time rather than
# silently at P2.M4's bus boundary.
_SEED_LIMIT = 2**64
_N_PATHS_LIMIT = 2**32


class ProvenanceError(RuntimeError):
    """Raised when neither a git repository nor `EXO_GIT_SHA` can establish a commit sha.

    Refusing to price on an indeterminate tree is deliberate: root CLAUDE.md invariant #7 makes a
    published number without a `git_sha` invalid, and silently reporting *some* sha in that
    situation (e.g. an empty string, or a sha of a different repo) would make the invariant
    unenforceable in exactly the case -- no `.git` directory, e.g. a stripped container image --
    where it matters most.
    """


# Anchor for git_sha()'s default `repo`: exo/src/exo/ -- inside the real hedging-desk checkout
# regardless of the CALLER's current working directory (P1-4, code review 2026-09-06). `git`
# discovers the repo root by walking UP from here, so this works from any commit depth.
_PACKAGE_DIR = Path(__file__).resolve().parent


def git_sha(*, repo: Path | None = None) -> str:
    """Return the current commit sha, suffixed "-dirty" when the working tree has changes.

    Overridable via the `EXO_GIT_SHA` environment variable for container builds that ship without
    a `.git` directory; an override is returned verbatim (dirty-tagging in that case is the build
    pipeline's responsibility, not this function's -- it has no working tree to inspect).

    Rationale for the "-dirty" suffix rather than either extreme: refusing to price on a dirty
    tree makes day-to-day development impossible, while silently reporting a clean sha for a dirty
    tree makes root CLAUDE.md invariant #7 a lie -- a number tagged with a sha that does not
    actually describe the code that produced it. The suffix is the honest middle: pricing still
    works, but the tag says the tree did not match a commit.

    `repo` (P1-4, code review 2026-09-06): the directory `git` runs in, defaulting to
    `_PACKAGE_DIR` (this file's own directory) rather than the PROCESS CWD. Running `git` with no
    `cwd=` reads whatever repository the caller's CWD happens to be in -- run the pricer from
    elsewhere and it silently stamps a DIFFERENT repo's sha onto a number, or raises
    `ProvenanceError` from a perfectly good checkout. A confidently wrong sha is worse than the
    `-dirty` case above already worries about. The parameter also makes this testable without
    `monkeypatch.chdir`.

    ponytail: the ceiling here is the "-dirty" suffix itself, not a refusal to price on a dirty
    tree. Trigger: P2.M4, the first milestone that PUBLISHES a number to the bus -- at that point a
    dirty-tree number reaching Delta One needs an explicit policy decision, not just an honest
    label.
    """
    override = os.environ.get("EXO_GIT_SHA")
    if override:
        return override
    repo_dir = repo if repo is not None else _PACKAGE_DIR
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
            text=True,
            cwd=repo_dir,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            check=True,
            text=True,
            cwd=repo_dir,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ProvenanceError(
            "no git repository found (via `git rev-parse HEAD`) and EXO_GIT_SHA is not set; "
            "cannot establish provenance for this run"
        ) from exc
    return f"{sha}-dirty" if status.strip() else sha


def _to_plain(value: object) -> object:
    """Recursively normalize any `Mapping`/list/tuple to plain `dict`/`list` for `json.dumps`.

    `json.dumps` only special-cases the concrete `dict` type; a non-`dict` `Mapping` (e.g. a
    `MappingProxyType`, or a pydantic model's nested config export) would otherwise raise
    `TypeError: Object of type X is not JSON serializable`. Nested mappings -- the per-underlying
    `[params.<SYMBOL>]` subtables -- go through this recursively.

    Mapping keys must be `str` (P1-1, security review MEDIUM, 2026-09-06): `params_hash`'s
    signature already declares `Mapping[str, object]`. An earlier version coerced every key with
    `str(k)` instead of enforcing that contract, which silently COLLIDES `{1: "x"}` and
    `{"1": "x"}`, and silently DROPS an entry when both `1` and `"1"` appear as keys in the same
    mapping (survivor by insertion order) -- `{1: "first", "1": "second"}` hashed the same as
    `{"1": "second"}`, with `"first"` gone. Not reachable from today's call sites (pydantic
    `model_dump()` and `tomllib` both yield `str` keys), but this function is documented as
    generic infrastructure for P2.M4's `bus/convert.py`, which could pass something else.
    """
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise TypeError(
                    f"params_hash requires all mapping keys to be str, got {k!r} of type "
                    f"{type(k).__name__}"
                )
            result[k] = _to_plain(v)
        return result
    if isinstance(value, list | tuple):
        return [_to_plain(v) for v in value]
    return value


def params_hash(obj: Mapping[str, object]) -> str:
    """sha256 hexdigest of a canonical JSON serialization of `obj`.

    Canonicalization is
    `json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)`:

    - `sort_keys=True` makes the hash invariant to dict insertion order, including in nested
      subtables (per-underlying parameter blocks), since `_to_plain` normalizes those to `dict`
      before serialization too.
    - `separators=(",", ":")` fixes the exact byte string (no incidental whitespace).
    - Floats render via `float.__repr__`, the shortest decimal string that round-trips to the same
      IEEE-754 double -- deterministic across platforms and Python versions for a given double,
      unlike a fixed-precision format (which can lose bits) or `str()` (locale-dependent in
      general, though CPython's float `str`/`repr` happen to coincide since 3.1).
    - `allow_nan=False` turns a NaN or +/-inf parameter into a raised `ValueError` at hash time --
      a loud, immediate failure -- rather than json's default of silently hashing the non-JSON
      token `NaN`/`Infinity`, which would let a broken calibration produce a `params_hash` that
      looks valid.
    - `-0.0` and `0.0` hash DIFFERENTLY (`repr(-0.0) == "-0.0" != "0.0" == repr(0.0)`). This is
      intentional: they are distinct IEEE-754 bit patterns, and this function fingerprints the
      exact parameters used, not their mathematical value.
    """
    canonical = json.dumps(_to_plain(obj), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EngineSettings:
    """The `[engine]` subtable of a run manifest."""

    scheme: str
    n_steps: int
    antithetic: bool


@dataclass(frozen=True)
class RunManifest:
    """A pricing run's full reproducibility record, read from / written to TOML.

    Seeds are READ FROM the manifest, never derived from time (exo/CLAUDE.md rule 1). TOML shape
    (written under `exo/run-manifests/`, matching `exo.toml`'s `seed_manifest` setting)::

        schema_version = 1
        run_id      = "2026-09-05T12-00-00Z-a1b2c3d4"
        created_ns  = 1757068800000000000
        git_sha     = "abc...def-dirty"
        model_id    = "heston-qe-v1"
        params_hash = "sha256hex..."
        seed        = 20260905
        n_paths     = 200000

        [engine]
        scheme = "qe"
        n_steps = 252
        antithetic = true

        [params.AAPL]
        s0 = 187.5
        # ... the eight Heston parameters echoed so params_hash is auditable
    """

    schema_version: int
    run_id: str
    created_ns: int
    git_sha: str
    model_id: str
    params_hash: str
    seed: int
    n_paths: int
    engine: EngineSettings
    params: Mapping[str, Mapping[str, float]]

    def __post_init__(self) -> None:
        # P2 (code review 2026-09-06): reject an unknown schema_version at construction time --
        # applies to RunManifest.read() too, since it constructs through this same dataclass --
        # rather than silently reading a future v2 manifest's fields as if they were v1's.
        if self.schema_version != 1:
            raise ValueError(
                f"unsupported schema_version={self.schema_version}: this reader only "
                "understands schema_version=1"
            )
        # Amendment A4: `seed`/`n_paths` are `uint64`/`uint32` in the proto (common.proto). Reject
        # values that would not fit at manifest construction time, not silently at P2.M4's bus
        # boundary.
        if not 0 <= self.seed < _SEED_LIMIT:
            raise ValueError(
                f"seed {self.seed} does not fit the proto's uint64 seed field "
                f"(must satisfy 0 <= seed < 2**64)"
            )
        if not 0 <= self.n_paths < _N_PATHS_LIMIT:
            raise ValueError(
                f"n_paths {self.n_paths} does not fit the proto's uint32 n_paths field "
                f"(must satisfy 0 <= n_paths < 2**32)"
            )

    @classmethod
    def read(cls, path: Path) -> Self:
        raw = tomllib.loads(path.read_text())
        engine_raw = raw["engine"]
        engine = EngineSettings(
            scheme=engine_raw["scheme"],
            n_steps=engine_raw["n_steps"],
            antithetic=engine_raw["antithetic"],
        )
        params_raw = raw.get("params", {})
        params = {symbol: dict(values) for symbol, values in params_raw.items()}
        return cls(
            schema_version=raw["schema_version"],
            run_id=raw["run_id"],
            created_ns=raw["created_ns"],
            git_sha=raw["git_sha"],
            model_id=raw["model_id"],
            params_hash=raw["params_hash"],
            seed=raw["seed"],
            n_paths=raw["n_paths"],
            engine=engine,
            params=params,
        )

    def write(self, path: Path) -> None:
        doc: dict[str, object] = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "created_ns": self.created_ns,
            "git_sha": self.git_sha,
            "model_id": self.model_id,
            "params_hash": self.params_hash,
            "seed": self.seed,
            "n_paths": self.n_paths,
            "engine": {
                "scheme": self.engine.scheme,
                "n_steps": self.engine.n_steps,
                "antithetic": self.engine.antithetic,
            },
            "params": {symbol: dict(values) for symbol, values in self.params.items()},
        }
        path.write_text(tomli_w.dumps(doc))


@dataclass(frozen=True)
class ValuationMetaFields:
    """Plain (non-proto) carrier for root CLAUDE.md invariant #7's reproducibility triple.

    Field names and order deliberately mirror `protocol/proto/common.proto`'s `ValuationMeta`
    message exactly: `model_id`, `params_hash`, `seed`, `n_paths`, `git_sha`. That structural
    parity is asserted by `exo/tests/test_provenance.py` against the generated proto descriptor,
    so P2.M4's `bus/convert.py` can be a plain field-by-field copy and a future proto field
    addition fails this module's test loudly instead of silently under-copying.
    """

    model_id: str
    params_hash: str
    seed: int
    n_paths: int
    git_sha: str


def to_valuation_meta(manifest: RunManifest) -> ValuationMetaFields:
    """Extract the reproducibility triple from a `RunManifest`."""
    return ValuationMetaFields(
        model_id=manifest.model_id,
        params_hash=manifest.params_hash,
        seed=manifest.seed,
        n_paths=manifest.n_paths,
        git_sha=manifest.git_sha,
    )
