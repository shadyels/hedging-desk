"""Random number source for the Monte Carlo engine — THE CRN SEAM.

`RandomSource.normals(shape, stream=...)` / `.uniforms(shape, stream=...)` are keyed by
a STREAM NAME, not by call order. The generator backing a stream is built from
`SeedSequence(entropy=seed, spawn_key=(h,))` where `h` is derived deterministically
from the stream name (the first 8 bytes of `sha256(stream_name)`, as a big-endian
int). Consequently `normals(shape, stream="spot")` returns the SAME array for a given
seed regardless of what else was drawn, in what order, or how many other streams
exist.

Why this matters: P2.M4 computes Greeks by bump-and-revalue with common random
numbers — a base run and a bumped run must share every random draw except the one
being perturbed. Under POSITIONAL spawning (`SeedSequence.spawn()` called in draw
order), a bumped revaluation that draws in a different order — or a scheme change
that adds one extra draw anywhere upstream — silently shifts every subsequent
spawned stream. The bump pair then stops sharing random numbers, the "Greek" becomes
a Monte-Carlo difference between two differently-seeded simulations rather than a
sensitivity, and NOTHING FAILS LOUDLY: the numbers are just wrong, quietly, forever.
Named streams keyed by content (the stream name) rather than position make this
class of bug structurally impossible.

It also keeps schemes independent of each other: QE draws `uniforms("variance")` +
`normals("spot")`; full-truncation Euler draws `normals("variance")` +
`normals("spot")`. Neither scheme's draws perturb the other's stream.

Slice 3 adds `SobolRandomSource`, a scrambled-Sobol QMC `RandomSource`. It does NOT
reuse `_stream_generator`'s per-name-hash design: hashing the stream name into a
per-stream `qmc.Sobol` instance (as `PseudoRandomSource` does for its PCG64
generator) would give "variance" and "spot" independently-scrambled — and thus
rank-correlated or identical — low-discrepancy point sets, silently coupling QE's
variance uniforms to its spot normals. Instead `SobolRandomSource` draws ONE Sobol
point set of total dimension `D = sum(n_dims for _, n_dims in dims)` and partitions
it into per-stream column blocks by the caller-declared `dims` layout — column
independence across streams is a property of one Sobol net's own dimensions, not of
scrambling separately per stream.

STATELESS BY DESIGN, undocumented until now (P1-5, code review 2026-09-06): `_draw` builds a
fresh `Generator` from `(seed, stream)` on every call and does not mutate `self`. A SECOND CALL
TO THE SAME STREAM THEREFORE RETURNS THE IDENTICAL ARRAY -- this is not a cache, it is the CRN
property itself, but it means callers must draw each stream exactly once per `simulate()` call
(as `heston.py` does) rather than assuming repeated calls advance the stream. M2/M4 authors
reaching for `rng.normals(shape, stream="x")` a second time expecting NEW numbers will silently
get the same ones back.

ponytail: stream reproducibility (the golden numbers this module lets us commit to
tests, and any pinned regression value derived from it) relies on NumPy's own
promise that a given `Generator` + `BitGenerator` + seed produces the same stream —
NEP 19 guarantees this for the legacy `RandomState` API but explicitly does NOT
extend that guarantee to `Generator`. Every bitwise-equality assertion in this
package is therefore reproducible only within the pinned `numpy==2.2.*` range
(pyproject.toml). Ceiling: that pin. Trigger: bumping numpy's major/minor version —
re-run the scheme-convergence study and re-pin any committed golden values.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import cached_property
from typing import Protocol

import numpy as np
from numpy.typing import NDArray
from scipy.special import ndtri  # type: ignore[import-untyped]
from scipy.stats import qmc  # type: ignore[import-untyped]

from exo.models.bridge import bridge_normals

_SOBOL_MAX_DIMS = 21201


def _stream_spawn_key(stream: str) -> int:
    """Derive a deterministic spawn key from a stream name.

    Uses the first 8 bytes of sha256(stream) as a big-endian unsigned int, so the
    key depends only on the stream's NAME (content), never on call order.
    """
    digest = hashlib.sha256(stream.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big")


def _stream_generator(seed: int, stream: str) -> np.random.Generator:
    seed_sequence = np.random.SeedSequence(entropy=seed, spawn_key=(_stream_spawn_key(stream),))
    return np.random.Generator(np.random.PCG64(seed_sequence))


class RandomSource(Protocol):
    """A source of named, independent random streams for the MC engine."""

    def normals(self, shape: tuple[int, ...], *, stream: str) -> NDArray[np.float64]:
        """Draw standard normal variates for `stream`."""
        ...

    def uniforms(self, shape: tuple[int, ...], *, stream: str) -> NDArray[np.float64]:
        """Draw uniform(0, 1) variates for `stream`."""
        ...

    @property
    def seed(self) -> int:
        """The base seed this source was constructed with."""
        ...

    @property
    def antithetic(self) -> bool:
        """Whether this source mirrors draws for antithetic variance reduction.

        `simulate()` (heston.py) requires this to equal the `EngineConfig.antithetic` it was
        given (P1-5, code review 2026-09-06): a mismatch would draw mirrored samples into a
        bundle tagged `antithetic=False` (or vice versa), silently routing `mc_estimate` to the
        wrong -- and in the dangerous direction, WRONGLY LOOSER -- standard-error formula.
        """
        ...

    @property
    def low_discrepancy(self) -> bool:
        """Whether this source produces a low-discrepancy (quasi-random) point set.

        True means the draws are NOT independent samples, so the plain sample standard
        error is invalid — `mc_estimate` raises on such a bundle and `rqmc_estimate`
        (a between-replicate SE over independent scrambles) must be used instead.
        """
        ...


@dataclass(frozen=True)
class PseudoRandomSource:
    """`RandomSource` backed by NumPy's PCG64, with source-level antithetic pairing.

    Antithetics are a property of the SOURCE, applied uniformly to every draw: the
    first `n_pairs = shape[0] // 2` rows are drawn fresh, and the remaining rows are
    the exact mirror — `Z -> -Z` for normals, `u -> 1 - u` for uniforms (the exact
    antithetic of `ndtri(u)`, so QE's quadratic branch gets an exact antithetic pair
    too). The leading dimension must be even when `antithetic` is True.
    """

    seed: int
    antithetic: bool = True

    @property
    def low_discrepancy(self) -> bool:
        """Always False: PCG64 draws are pseudo-random, not a low-discrepancy sequence."""
        return False

    def _draw(
        self,
        shape: tuple[int, ...],
        stream: str,
        sampler: str,
    ) -> NDArray[np.float64]:
        generator = _stream_generator(self.seed, stream)
        if not self.antithetic:
            result = _sample(generator, sampler, shape)
            return _clamp_open_unit_interval(result) if sampler == "uniform" else result

        if shape[0] % 2 != 0:
            raise ValueError(
                f"antithetic PseudoRandomSource requires an even leading dimension, "
                f"got shape={shape}"
            )
        n_pairs = shape[0] // 2
        half_shape = (n_pairs, *shape[1:])
        base = _sample(generator, sampler, half_shape)
        mirror = _mirror(sampler, base)
        combined = np.concatenate([base, mirror], axis=0)
        # Clamp AFTER concatenation, not inside _sample before mirroring (SHOULD-4, third
        # code-review round, 2026-09-06): clamping `base` alone does not close the MIRROR path --
        # `1.0 - np.nextafter(0.0, 1.0)` rounds to EXACTLY `1.0` in float64 (the clamp value is far
        # smaller than 1.0's ULP), so a raw `0.0` draw's mirror stayed exactly `1.0` and reached
        # QE's exponential branch's `(1-p)/(1-u)` division unclamped. Clamping the concatenated
        # result closes both the base draw's own boundary and its mirror's.
        return _clamp_open_unit_interval(combined) if sampler == "uniform" else combined

    def normals(self, shape: tuple[int, ...], *, stream: str) -> NDArray[np.float64]:
        return self._draw(shape, stream, "normal")

    def uniforms(self, shape: tuple[int, ...], *, stream: str) -> NDArray[np.float64]:
        return self._draw(shape, stream, "uniform")


def _sample(
    generator: np.random.Generator, sampler: str, shape: tuple[int, ...]
) -> NDArray[np.float64]:
    if sampler == "normal":
        return generator.standard_normal(size=shape)
    return generator.uniform(low=0.0, high=1.0, size=shape)


def _clamp_open_unit_interval(u: NDArray[np.float64]) -> NDArray[np.float64]:
    """Clamp uniform draws strictly inside the OPEN interval (0, 1) (P2, code review 2026-09-06;
    moved to run on `_draw`'s CONCATENATED result rather than inside `_sample`, SHOULD-4, third
    code-review round, 2026-09-06 -- see `_draw`'s comment for why: clamping the pre-mirror
    `base` alone does not close the antithetic MIRROR's boundary). `Generator.uniform`'s
    half-open `[0, 1)` can return exactly `0.0` (probability ~2**-53, rare but not zero);
    `heston.py`'s QE scheme computes `ndtri(u)` (`-inf` at `u=0.0`) in the quadratic branch and
    `(1-p)/(1-u)` (division by zero at `u=1.0`) in the exponential branch -- the latter reachable
    via the antithetic mirror `1 - 0.0 == 1.0` even though the raw draw itself never returns
    `1.0`. `np.nextafter` moves either endpoint the smallest representable float64 step inward:
    noise relative to any real MC estimate, and closes both failure modes for free.
    """
    lo = np.nextafter(np.float64(0.0), np.float64(1.0))
    hi = np.nextafter(np.float64(1.0), np.float64(0.0))
    return np.clip(u, lo, hi)


def _mirror(sampler: str, base: NDArray[np.float64]) -> NDArray[np.float64]:
    if sampler == "normal":
        return -base
    return 1.0 - base


@dataclass(frozen=True, eq=False)
class SobolRandomSource:
    """`RandomSource` backed by a single scrambled-Sobol low-discrepancy point set,
    partitioned into per-stream column blocks by `dims` (ADR-006 Amendment 6).

    ONE Sobol point set, not one per stream (see module docstring): scrambling per
    stream name would rank-correlate independently-scrambled nets across streams --
    each is individually low-discrepancy, but nothing makes two SEPARATELY scrambled
    nets jointly independent. Column independence across streams instead comes from
    disjoint dimensions of the SAME net, which is what the `dims` column-block
    partition gives.

    `antithetic` is hardwired False as a property, not a field (ADR-006 Amendment
    6): antithetic pairing (Z -> -Z) of a scrambled Sobol net destroys the
    (t, m, s)-net equidistribution that motivated using Sobol in the first place, and
    the two error estimators are incompatible objects -- `PseudoRandomSource`'s
    pair-mean SE over antithetic pairs vs RQMC's between-replicate SE over
    independent scrambles (see `low_discrepancy`). `simulate()`'s existing
    `engine.antithetic != rng.antithetic` guard (heston.py) then forces
    `EngineConfig(antithetic=False)` for every QMC run for free.

    The scramble is derived from `(seed, replicate)` ONLY -- never any model
    parameter -- mirroring `_stream_generator`'s name-hash mechanism but keyed by a
    fixed literal ("sobol-scramble") plus `replicate`. This is the P2.M4
    common-random-numbers property: a bump-and-revalue pair sharing
    `(seed, replicate, dims, n_paths)` gets a bitwise identical point set.
    """

    seed: int
    n_paths: int
    # ponytail: layout is caller-declared and only validated against the requested
    # draw shape, not derived from the scheme. Ceiling: the two streams simulate()
    # draws today ("variance", "spot"). Upgrade: derive the layout from
    # EngineConfig. Trigger: a scheme or product drawing a third stream (P2.M2 LSM
    # exercise dates).
    dims: tuple[tuple[str, int], ...]
    replicate: int = 0
    bridge: frozenset[str] = frozenset()
    t: NDArray[np.float64] | None = None

    def __post_init__(self) -> None:
        if self.n_paths <= 0 or (self.n_paths & (self.n_paths - 1)) != 0:
            raise ValueError(
                "SobolRandomSource requires n_paths to be a power of two, got "
                f"n_paths={self.n_paths}"
            )
        names = [name for name, _ in self.dims]
        if len(names) != len(set(names)):
            raise ValueError(f"SobolRandomSource.dims has duplicate stream names: {names}")
        for name, n_dims in self.dims:
            if n_dims < 1:
                raise ValueError(
                    f"SobolRandomSource.dims stream {name!r} has n_dims={n_dims}, must be >= 1"
                )
        total_dims = sum(n_dims for _, n_dims in self.dims)
        if total_dims > _SOBOL_MAX_DIMS:
            raise ValueError(
                f"SobolRandomSource total dims={total_dims} exceeds scipy Sobol's cap of "
                f"{_SOBOL_MAX_DIMS}"
            )
        if self.bridge:
            if self.t is None:
                raise ValueError(
                    "SobolRandomSource.bridge is non-empty but t is None -- t is required "
                    "when any stream is bridged"
                )
            undeclared = self.bridge - set(names)
            if undeclared:
                raise ValueError(
                    f"SobolRandomSource.bridge names streams not in dims: {sorted(undeclared)} "
                    f"-- declared streams are {names}"
                )

    @cached_property
    def _offsets(self) -> dict[str, tuple[int, int]]:
        """stream name -> (column offset, n_dims); offsets accumulate in `dims` order."""
        offsets: dict[str, tuple[int, int]] = {}
        offset = 0
        for name, n_dims in self.dims:
            offsets[name] = (offset, n_dims)
            offset += n_dims
        return offsets

    @cached_property
    def _points(self) -> NDArray[np.float64]:
        """The full (n_paths, D) scrambled-Sobol uniform point matrix, built once.

        ponytail: eager materialisation of the full (n_paths, D) matrix, held for the
        whole `simulate()` call, on top of heston.py's five array-equivalents.
        Ceiling: <MEASURED IN T9> -- a later task fills this in from a real
        tracemalloc run. Upgrade: generate per-stream blocks on demand instead of one
        eager (n_paths, D) matrix. Trigger: P2.M2 LSM retention, or a batch that will
        not fit the study's --max-batch-bytes.
        """
        total_dims = sum(n_dims for _, n_dims in self.dims)
        seed_sequence = np.random.SeedSequence(
            entropy=self.seed, spawn_key=(_stream_spawn_key("sobol-scramble"), self.replicate)
        )
        generator = np.random.Generator(np.random.PCG64(seed_sequence))
        sampler = qmc.Sobol(d=total_dims, scramble=True, rng=generator)
        # ponytail: random_base2 only -- non-power-of-two n_paths raises rather than
        # padding or truncating (enforced in __post_init__). Ceiling: RQMC path
        # budgets are restricted to 2**m, so an RQMC gate cannot match a pseudo
        # gate's exact path count. Upgrade: pad to the next power of two and discard
        # the excess, accepting the balance loss. Trigger: a gate needing a
        # non-power-of-two budget.
        m = self.n_paths.bit_length() - 1
        points: NDArray[np.float64] = sampler.random_base2(m)
        return points

    def _block(self, shape: tuple[int, ...], stream: str) -> NDArray[np.float64]:
        if stream not in self._offsets:
            raise ValueError(
                f"SobolRandomSource stream={stream!r} is not declared in dims -- declared "
                f"streams are {[name for name, _ in self.dims]}"
            )
        if len(shape) != 2:
            raise ValueError(f"SobolRandomSource draws require a 2-D shape, got shape={shape}")
        if shape[0] != self.n_paths:
            raise ValueError(
                f"SobolRandomSource shape[0]={shape[0]} does not match n_paths={self.n_paths}"
            )
        offset, n_dims = self._offsets[stream]
        if shape[1] != n_dims:
            raise ValueError(
                f"SobolRandomSource stream={stream!r} shape[1]={shape[1]} does not match "
                f"declared n_dims={n_dims}"
            )
        return self._points[:, offset : offset + n_dims]

    def normals(self, shape: tuple[int, ...], *, stream: str) -> NDArray[np.float64]:
        u = _clamp_open_unit_interval(self._block(shape, stream))
        z: NDArray[np.float64] = ndtri(u)
        if stream in self.bridge:
            if self.t is None:  # pragma: no cover - enforced in __post_init__
                raise ValueError("SobolRandomSource.t is None despite a declared bridge stream")
            z = bridge_normals(z, self.t)
        return z

    def uniforms(self, shape: tuple[int, ...], *, stream: str) -> NDArray[np.float64]:
        if stream in self.bridge:
            raise ValueError(
                f"SobolRandomSource stream={stream!r} is bridged -- bridging applies to "
                "normals only, not uniforms"
            )
        return _clamp_open_unit_interval(self._block(shape, stream))

    @property
    def antithetic(self) -> bool:
        return False

    @property
    def low_discrepancy(self) -> bool:
        return True
