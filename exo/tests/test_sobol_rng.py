"""Tests for exo.models.rng.SobolRandomSource — scrambled-Sobol QMC RandomSource.

See rng.py's module docstring and this task's brief for the bug this source exists
to prevent: a per-stream-name-hashed Sobol instance (mirroring PseudoRandomSource's
design) would give "variance" and "spot" rank-correlated point sets. The fix is one
Sobol point set of total dimension D, partitioned into per-stream column blocks by
an explicit declared layout (`dims`).
"""

from __future__ import annotations

import numpy as np
import pytest

from exo.models.heston import simulate
from exo.models.params import EngineConfig, HestonParams
from exo.models.rng import PseudoRandomSource, RandomSource, SobolRandomSource

_PARAMS = HestonParams(s0=100.0, r=0.02, q=0.0, v0=0.04, kappa=1.5, theta=0.04, xi=0.3, rho=-0.7)


def test_single_point_set_columns_are_not_rank_correlated_across_streams() -> None:
    """THE catastrophic-bug test: a layout bug (per-stream hashed Sobol instances,
    mirroring PseudoRandomSource) makes variance and spot streams identical or
    rank-correlated. The correct fix (one point set, column-block partition) does
    not."""
    source = SobolRandomSource(seed=1, n_paths=4096, dims=(("variance", 8), ("spot", 8)))
    variance = source.normals((4096, 8), stream="variance")
    spot = source.normals((4096, 8), stream="spot")

    assert not np.array_equal(variance, spot)
    for j in range(8):
        corr = np.corrcoef(variance[:, j], spot[:, j])[0, 1]
        assert abs(corr) < 0.1, f"column {j}: correlation {corr} too high"


def test_crn_bitwise_identical_for_same_seed_dims_replicate() -> None:
    dims = (("variance", 4), ("spot", 4))
    a = SobolRandomSource(seed=7, n_paths=1024, dims=dims, replicate=0)
    b = SobolRandomSource(seed=7, n_paths=1024, dims=dims, replicate=0)
    za_first = a.normals((1024, 4), stream="variance")
    za_second = b.normals((1024, 4), stream="variance")
    assert np.array_equal(za_first, za_second)
    assert np.array_equal(a.normals((1024, 4), stream="spot"), b.normals((1024, 4), stream="spot"))


def test_crn_differs_across_replicates() -> None:
    """Different replicate -> different (not bitwise-equal) point set.

    NOTE (deviation from brief, verified empirically): the brief additionally asked
    for "replicate 0 vs replicate 1, column 0, correlation < 0.1". scipy's Sobol
    `scramble=True` uses LMS (linear matrix scramble) + digital shift, not full Owen
    (nested-uniform) scrambling. LMS's per-dimension scramble matrix is LOWER
    TRIANGULAR, so a column's leading (most-significant) output bit is a function of
    only the first few scramble-matrix bits -- for many dimension/seed pairs this
    makes the leading bit's match/mismatch pattern between two INDEPENDENT scrambles
    constant across the whole point set (not ~50/50 per point), which drives
    |Pearson correlation| to 0.5-0.95 for a large fraction of (seed, dim) pairs
    rather than the ~0 a naive "independent scrambles" assumption predicts. This
    reproduces for every column tested (0-7) and is independent of `m`/`n_paths`, so
    it is not a bug in this implementation -- it is a structural property of scipy's
    LMS scrambling. Verified against `qmc.Sobol` directly (bypassing this module)
    with ~20 seed pairs, corr in [-0.95, 0.95], never all below 0.1. Bitwise
    inequality is the correct, achievable CRN check; the correlation bound is
    dropped."""
    dims = (("variance", 4), ("spot", 4))
    a = SobolRandomSource(seed=7, n_paths=1024, dims=dims, replicate=0)
    b = SobolRandomSource(seed=7, n_paths=1024, dims=dims, replicate=1)
    za = a.normals((1024, 4), stream="variance")
    zb = b.normals((1024, 4), stream="variance")
    assert not np.array_equal(za, zb)


def test_n_paths_must_be_power_of_two() -> None:
    with pytest.raises(ValueError, match="power of two"):
        SobolRandomSource(seed=1, n_paths=1000, dims=(("spot", 2),))


def test_total_dims_cap() -> None:
    with pytest.raises(ValueError, match="21201"):
        SobolRandomSource(seed=1, n_paths=1024, dims=(("spot", 21202),))


def test_duplicate_stream_name_raises() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        SobolRandomSource(seed=1, n_paths=1024, dims=(("spot", 2), ("spot", 2)))


def test_zero_dims_for_a_stream_raises() -> None:
    with pytest.raises(ValueError, match="n_dims"):
        SobolRandomSource(seed=1, n_paths=1024, dims=(("spot", 0),))


def test_bridge_without_t_raises() -> None:
    with pytest.raises(ValueError, match="t"):
        SobolRandomSource(seed=1, n_paths=1024, dims=(("spot", 2),), bridge=frozenset({"spot"}))


def test_bridge_stream_not_in_dims_raises() -> None:
    t = np.array([0.0, 0.5, 1.0])
    with pytest.raises(ValueError, match="bridge"):
        SobolRandomSource(
            seed=1, n_paths=1024, dims=(("spot", 2),), bridge=frozenset({"variance"}), t=t
        )


def test_undeclared_stream_raises_on_draw() -> None:
    source = SobolRandomSource(seed=1, n_paths=1024, dims=(("spot", 2),))
    with pytest.raises(ValueError, match="spot"):
        source.normals((1024, 2), stream="variance")


def test_wrong_shape_ndim_raises() -> None:
    source = SobolRandomSource(seed=1, n_paths=1024, dims=(("spot", 2),))
    with pytest.raises(ValueError, match="shape"):
        source.normals((1024,), stream="spot")


def test_wrong_n_paths_in_shape_raises() -> None:
    source = SobolRandomSource(seed=1, n_paths=1024, dims=(("spot", 2),))
    with pytest.raises(ValueError, match="n_paths"):
        source.normals((512, 2), stream="spot")


def test_wrong_n_dims_in_shape_raises() -> None:
    source = SobolRandomSource(seed=1, n_paths=1024, dims=(("spot", 2),))
    with pytest.raises(ValueError, match="n_dims"):
        source.normals((1024, 3), stream="spot")


def test_uniforms_strictly_inside_open_unit_interval() -> None:
    source = SobolRandomSource(seed=1, n_paths=1024, dims=(("spot", 2),))
    u = source.uniforms((1024, 2), stream="spot")
    assert (u > 0.0).all()
    assert (u < 1.0).all()


def test_normals_marginal_sanity() -> None:
    source = SobolRandomSource(seed=1, n_paths=4096, dims=(("spot", 4),))
    z = source.normals((4096, 4), stream="spot")
    tol = 4.0 / np.sqrt(4096)
    assert np.all(np.abs(z.mean(axis=0)) < tol)
    assert np.all(np.abs(z.var(axis=0) - 1.0) < 0.05)


def test_properties_and_protocol_conformance() -> None:
    source = SobolRandomSource(seed=1, n_paths=1024, dims=(("spot", 2),))
    assert source.antithetic is False
    assert source.low_discrepancy is True
    assert source.seed == 1
    typed: RandomSource = source
    assert typed is source


def test_end_to_end_simulate_stamps_low_discrepancy() -> None:
    engine = EngineConfig(scheme="qe", n_steps=8, n_paths=4096, expiry=1.0, antithetic=False)
    source = SobolRandomSource(seed=1, n_paths=4096, dims=(("variance", 8), ("spot", 8)))
    bundle = simulate(_PARAMS, engine, source)
    assert bundle.low_discrepancy is True


def test_end_to_end_simulate_raises_on_antithetic_mismatch() -> None:
    engine = EngineConfig(scheme="qe", n_steps=8, n_paths=4096, expiry=1.0, antithetic=True)
    source = SobolRandomSource(seed=1, n_paths=4096, dims=(("variance", 8), ("spot", 8)))
    with pytest.raises(ValueError, match="antithetic"):
        simulate(_PARAMS, engine, source)


def test_bridged_stream_shape_and_finite() -> None:
    t = np.linspace(0.0, 1.0, 9)
    source = SobolRandomSource(
        seed=1, n_paths=1024, dims=(("spot", 8),), bridge=frozenset({"spot"}), t=t
    )
    z = source.normals((1024, 8), stream="spot")
    assert z.shape == (1024, 8)
    assert np.all(np.isfinite(z))


def test_bridged_stream_via_uniforms_raises() -> None:
    t = np.linspace(0.0, 1.0, 9)
    source = SobolRandomSource(
        seed=1, n_paths=1024, dims=(("spot", 8),), bridge=frozenset({"spot"}), t=t
    )
    with pytest.raises(ValueError, match="bridge"):
        source.uniforms((1024, 8), stream="spot")


def test_pseudo_random_source_unaffected_baseline() -> None:
    """Sanity: adding SobolRandomSource does not touch PseudoRandomSource behavior."""
    source = PseudoRandomSource(seed=1, antithetic=False)
    z = source.normals((8,), stream="spot")
    assert z.shape == (8,)
