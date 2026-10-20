"""Shared hand-built `PathBundle` helper for the P2.M2 product unit tests."""

from __future__ import annotations

import numpy as np

from exo.models.heston import PathBundle


def forced_bundle(paths: list[list[float]], expiry: float = 1.0, v: float = 0.04) -> PathBundle:
    """A `PathBundle` with the given spot paths on a uniform grid over `[0, expiry]`."""
    s = np.asarray(paths, dtype=np.float64)
    return PathBundle(
        t=np.linspace(0.0, expiry, s.shape[1], dtype=np.float64),
        S=s,
        v=np.full_like(s, v),
        antithetic=False,
        n_pairs=None,
        qe_fallback_count=None,
        low_discrepancy=False,
    )
