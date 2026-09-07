"""Tests for exo.models.rng — the common-random-number (CRN) seam.

These tests exist to protect one property: `normals(shape, stream="x")` returns the
same array for a given seed regardless of what else was drawn, in what order. See
exo/src/exo/models/rng.py's module docstring for why (P2.M4 bump-and-revalue Greeks).
"""

import numpy as np
import pytest
from numpy.typing import NDArray

from exo.models.rng import PseudoRandomSource


def test_same_seed_same_stream_gives_identical_normals_regardless_of_call_order() -> None:
    """The defining CRN property: draw two streams in opposite orders from two
    independently constructed sources sharing a seed, and the SAME stream must come
    back bitwise identical no matter which order it was drawn in."""
    source_a = PseudoRandomSource(seed=1234, antithetic=False)
    source_b = PseudoRandomSource(seed=1234, antithetic=False)

    # source_a draws "spot" first, then "variance"
    a_spot = source_a.normals((4, 3), stream="spot")
    a_variance = source_a.normals((4, 3), stream="variance")

    # source_b draws "variance" first, then "spot" — reversed order
    b_variance = source_b.normals((4, 3), stream="variance")
    b_spot = source_b.normals((4, 3), stream="spot")

    assert np.array_equal(a_spot, b_spot)
    assert np.array_equal(a_variance, b_variance)


def test_distinct_streams_differ() -> None:
    source = PseudoRandomSource(seed=42, antithetic=False)
    spot = source.normals((8,), stream="spot")
    variance = source.normals((8,), stream="variance")
    assert not np.array_equal(spot, variance)


def test_seed_is_exposed() -> None:
    source = PseudoRandomSource(seed=777, antithetic=False)
    assert source.seed == 777


def test_antithetic_normals_reflection_is_exact() -> None:
    source = PseudoRandomSource(seed=99, antithetic=True)
    z = source.normals((10, 2), stream="spot")
    assert z.shape == (10, 2)
    n_pairs = 5
    assert np.array_equal(z[n_pairs:], -z[:n_pairs])


def test_antithetic_uniforms_reflection_is_exact() -> None:
    source = PseudoRandomSource(seed=99, antithetic=True)
    u = source.uniforms((10, 2), stream="variance")
    assert u.shape == (10, 2)
    n_pairs = 5
    assert np.array_equal(u[n_pairs:], 1.0 - u[:n_pairs])


def test_antithetic_requires_even_leading_dimension() -> None:
    source = PseudoRandomSource(seed=1, antithetic=True)
    with pytest.raises(ValueError, match="even"):
        source.normals((5, 2), stream="spot")


def test_non_antithetic_source_does_not_mirror() -> None:
    source = PseudoRandomSource(seed=1, antithetic=False)
    z = source.normals((6,), stream="spot")
    # no pairing structure imposed: odd leading dim is fine
    z_odd = source.normals((5,), stream="spot")
    assert z.shape == (6,)
    assert z_odd.shape == (5,)


def test_uniforms_are_strictly_inside_the_open_unit_interval() -> None:
    """P2 (code review 2026-09-06): `Generator.uniform` can return exactly 0.0 (probability
    ~2**-53, but reachable over enough draws). `ndtri(0.0) == -inf`, and QE's exponential branch
    computes `(1-p)/(1-u)`, which divides by zero at `u == 1.0` -- reachable via the antithetic
    mirror `1 - 0.0 == 1.0`. Draw enough samples that hitting either float64 boundary by chance
    is not what this test is relying on: it instead pins the CLAMP directly."""
    from exo.models.rng import _clamp_open_unit_interval

    u = np.array([0.0, 0.25, 0.5, 0.75, 1.0])
    clamped = _clamp_open_unit_interval(u)
    assert clamped[0] > 0.0
    assert clamped[-1] < 1.0
    assert np.array_equal(clamped[1:-1], u[1:-1])  # interior values untouched


def test_uniforms_from_a_real_draw_never_hit_the_endpoints() -> None:
    source = PseudoRandomSource(seed=1, antithetic=True)
    u = source.uniforms((1000, 2), stream="variance")
    assert np.all(u > 0.0)
    assert np.all(u < 1.0)


def test_antithetic_mirror_of_a_zero_draw_is_clamped_away_from_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SHOULD-4 (third code-review round, 2026-09-06): the clamp ran inside `_sample`, i.e.
    BEFORE `_mirror` -- so a raw `0.0` draw got clamped to `lo = np.nextafter(0.0, 1.0)`
    (~5e-324), but `1.0 - lo` rounds to EXACTLY `1.0` in float64 (`lo` is far smaller than 1.0's
    ULP), so the antithetic MIRROR of a zero draw was never closed. QE's exponential branch
    computes `(1-p)/(1-u)`, dividing by zero at `u=1.0`. The fix clamps the CONCATENATED
    (base + mirror) result in `_draw`, after mirroring, not inside `_sample` before it.

    Forces the raw generator to return an all-zero draw (rather than relying on chance) by
    monkeypatching `_stream_generator`, so this test does not depend on ever actually rolling a
    literal `0.0` from PCG64."""
    import exo.models.rng as rng_module

    class _ZeroGenerator:
        def uniform(
            self, low: float, high: float, size: tuple[int, ...]
        ) -> NDArray[np.float64]:
            return np.zeros(size)

    monkeypatch.setattr(rng_module, "_stream_generator", lambda seed, stream: _ZeroGenerator())
    source = PseudoRandomSource(seed=1, antithetic=True)
    u = source.uniforms((2, 1), stream="variance")
    assert u.shape == (2, 1)
    assert np.all(u > 0.0), f"base draw not clamped away from 0: {u}"
    assert np.all(u < 1.0), f"antithetic mirror of a zero draw not clamped away from 1: {u}"
