"""Brownian-bridge reordering of iid standard normals for QMC dimension reduction.

NOT the other Brownian bridge in this codebase: `products/base.py`'s
`Monitoring.CONTINUOUS_BRIDGE` is a barrier-monitoring survival-probability
correction (discrete-to-continuous crossing adjustment). This module has nothing
to do with barriers — it reorders which input dimensions drive which points on
the simulation time grid, which is what makes scrambled-Sobol sequences effective
(Sobol's leading dimensions are the best-equidistributed; the bridge construction
assigns them to the coarsest, highest-variance features of the path — the
terminal point first, then successive midpoints — so QMC's advantage concentrates
where it matters most). See docs/GLOSSARY.md for the disambiguation.

A later task wires this into the Sobol source; this module is a standalone pure
function with no dependency on the sampler.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray


def bridge_normals(z: NDArray[np.float64], t: NDArray[np.float64]) -> NDArray[np.float64]:
    """Reorder iid standard normals by Brownian-bridge construction over grid `t`.

    z: (n_paths, n_steps) iid N(0,1). Column j is BRIDGE DIMENSION j, not time step j.
    t: (n_steps+1,) ascending time grid including t[0] == 0.
    Returns (n_paths, n_steps) standardised increments dW_i / sqrt(dt_i) — distributionally
    identical to the input, so it substitutes for a plain normals matrix directly.
    """
    if z.ndim != 2:
        raise ValueError(f"z must be 2-D (n_paths, n_steps), got ndim={z.ndim}")
    n_steps = z.shape[1]
    if n_steps + 1 != t.shape[0]:
        raise ValueError(
            f"z.shape[1]+1 must equal t.shape[0]: got z.shape[1]={n_steps} "
            f"(+1 = {n_steps + 1}) vs t.shape[0]={t.shape[0]}"
        )
    if not np.all(np.diff(t) > 0):
        raise ValueError("t must be strictly ascending")
    if t[0] != 0.0:
        raise ValueError(f"t[0] must be 0.0, got t[0]={t[0]}")

    n_paths = z.shape[0]
    w = np.zeros((n_paths, n_steps + 1))
    w[:, n_steps] = np.sqrt(t[n_steps]) * z[:, 0]

    # Binary subdivision schedule over indices [0, n_steps]: a Python loop over
    # ~n_steps intervals (not paths) building which (l, m, r) triple consumes the
    # next z column. Matches heston.py's "only loop is over time steps" convention.
    next_col = 1
    intervals = [(0, n_steps)]
    while intervals:
        next_intervals: list[tuple[int, int]] = []
        for left, right in intervals:
            if right - left < 2:
                continue
            mid = (left + right) // 2
            t_l, t_m, t_r = t[left], t[mid], t[right]
            w[:, mid] = ((t_r - t_m) * w[:, left] + (t_m - t_l) * w[:, right]) / (
                t_r - t_l
            ) + np.sqrt((t_m - t_l) * (t_r - t_m) / (t_r - t_l)) * z[:, next_col]
            next_col += 1
            next_intervals.append((left, mid))
            next_intervals.append((mid, right))
        intervals = next_intervals

    result: NDArray[np.float64] = np.diff(w, axis=1) / np.sqrt(np.diff(t))
    return result
