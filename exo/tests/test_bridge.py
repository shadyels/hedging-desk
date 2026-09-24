"""Tests for exo.models.bridge — the Brownian-bridge normals reordering.

See exo/src/exo/models/bridge.py's module docstring for why this exists (QMC
dimension reduction) and how it differs from products/base.py's
Monitoring.CONTINUOUS_BRIDGE (barrier survival-probability correction).
"""

import numpy as np
import pytest

from exo.models.bridge import bridge_normals


def test_orthogonal_on_identity_uniform_grid() -> None:
    """Feeding the identity matrix recovers bridge_normals' own linear map. An
    orthogonal map preserves the iid standard-normal distribution exactly, so
    this single assertion proves the bridge is distributionally a no-op without
    sampling anything."""
    t = np.linspace(0.0, 1.0, 17)
    m = bridge_normals(np.eye(16), t)
    max_err = np.max(np.abs(m @ m.T - np.eye(16)))
    assert max_err < 1e-12


def test_single_step_returns_input_unchanged() -> None:
    z = np.array([[0.5], [-1.2], [3.0]])
    t = np.array([0.0, 1.0])
    out = bridge_normals(z, t)
    assert np.allclose(out, z, atol=1e-12)


def test_orthogonal_on_nonuniform_grid() -> None:
    t = np.array([0.0, 0.1, 0.35, 0.4, 1.0])
    m = bridge_normals(np.eye(4), t)
    max_err = np.max(np.abs(m @ m.T - np.eye(4)))
    assert max_err < 1e-12


@pytest.mark.parametrize("n_steps", [5, 7])
def test_orthogonal_on_nonpower_of_two_steps(n_steps: int) -> None:
    t = np.linspace(0.0, 1.0, n_steps + 1)
    m = bridge_normals(np.eye(n_steps), t)
    max_err = np.max(np.abs(m @ m.T - np.eye(n_steps)))
    assert max_err < 1e-12


def test_rejects_mismatched_shapes() -> None:
    z = np.zeros((4, 5))
    t = np.linspace(0.0, 1.0, 5)  # needs 6 entries for 5 steps
    with pytest.raises(ValueError, match=r"5.*6|z\.shape\[1\]\+1.*t\.shape\[0\]"):
        bridge_normals(z, t)


def test_rejects_non_2d_z() -> None:
    z = np.zeros(5)
    t = np.linspace(0.0, 1.0, 6)
    with pytest.raises(ValueError, match="ndim"):
        bridge_normals(z, t)


def test_rejects_non_ascending_t() -> None:
    z = np.zeros((4, 3))
    t = np.array([0.0, 0.5, 0.4, 1.0])
    with pytest.raises(ValueError, match="ascending"):
        bridge_normals(z, t)


def test_rejects_t0_nonzero() -> None:
    z = np.zeros((4, 3))
    t = np.array([0.1, 0.4, 0.7, 1.0])
    with pytest.raises(ValueError, match="t\\[0\\]"):
        bridge_normals(z, t)


def test_output_shape_matches_input() -> None:
    rng = np.random.default_rng(42)
    z = rng.standard_normal((10, 8))
    t = np.linspace(0.0, 1.0, 9)
    out = bridge_normals(z, t)
    assert out.shape == z.shape
