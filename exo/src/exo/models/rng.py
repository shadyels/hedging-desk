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

Slice 3 will add a Sobol-sequence `RandomSource` implementing this same Protocol;
nothing here is QMC-specific and nothing should be added in anticipation of it.

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
from typing import Protocol

import numpy as np
from numpy.typing import NDArray


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
