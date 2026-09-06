"""Tests for exo.models.rng — the common-random-number (CRN) seam.

These tests exist to protect one property: `normals(shape, stream="x")` returns the
same array for a given seed regardless of what else was drawn, in what order. See
exo/src/exo/models/rng.py's module docstring for why (P2.M4 bump-and-revalue Greeks).
"""

import numpy as np
import pytest

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
