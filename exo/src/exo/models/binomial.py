"""Cox-Ross-Rubinstein binomial tree: European, or Bermudan on a subset of tree steps.

A gate-reference pricer (ADR-006 Amendment 7 s1), not a production pricer. Its job is to price
Bermudan exercise on EXACTLY the dates the LSM engine exercises on (`exercise_every` tree steps
per LSM grid step), so the LSM gate compares like with like: continuous-time American exceeds the
50-date Bermudan by ~0.014 at the G-W3 fixture (~0.7 SE), which would otherwise be misread as LSM
bias.
"""

from __future__ import annotations

import math
from typing import Literal

import numpy as np
from numpy.typing import NDArray


def crr_price(
    *,
    s0: float,
    strike: float,
    r: float,
    q: float,
    sigma: float,
    expiry: float,
    option_type: Literal["call", "put"],
    n_tree_steps: int,
    exercise_every: int | None,
) -> float:
    """CRR tree price. `exercise_every=None` is European; otherwise exercise is allowed at tree
    steps `n` with `n % exercise_every == 0` and `0 < n < n_tree_steps` (Bermudan; the terminal
    step is always an exercise date, as for any option).

    Raises unless `n_tree_steps % exercise_every == 0` (the exercise dates must tile the tree
    evenly) and the risk-neutral up-probability lies strictly in (0, 1).
    """
    if option_type not in ("call", "put"):
        raise ValueError(f"option_type must be 'call' or 'put', got {option_type!r}")
    for name, x in (("s0", s0), ("strike", strike), ("sigma", sigma), ("expiry", expiry)):
        if not math.isfinite(x) or x <= 0.0:
            raise ValueError(f"{name} must be a positive finite number, got {x}")
    if not math.isfinite(r) or not math.isfinite(q):
        raise ValueError(f"r and q must be finite, got r={r}, q={q}")
    if n_tree_steps < 1:
        raise ValueError(f"n_tree_steps must be >= 1, got {n_tree_steps}")
    if exercise_every is not None and (exercise_every < 1 or n_tree_steps % exercise_every != 0):
        raise ValueError(
            f"exercise_every={exercise_every} must be >= 1 and divide n_tree_steps={n_tree_steps}"
        )

    dt = expiry / n_tree_steps
    step = sigma * math.sqrt(dt)
    u = math.exp(step)
    d = 1.0 / u
    p = (math.exp((r - q) * dt) - d) / (u - d)
    if not 0.0 < p < 1.0:
        raise ValueError(f"risk-neutral up-probability p={p} not in (0, 1); refine the tree")
    disc = math.exp(-r * dt)
    sign = 1.0 if option_type == "call" else -1.0

    def intrinsic(n: int) -> NDArray[np.float64]:
        j = np.arange(n + 1, dtype=np.float64)
        spot = np.exp(math.log(s0) + (2.0 * j - n) * step)
        out: NDArray[np.float64] = np.maximum(sign * (spot - strike), 0.0)
        return out

    value = intrinsic(n_tree_steps)
    for n in range(n_tree_steps - 1, -1, -1):
        value = disc * (p * value[1:] + (1.0 - p) * value[:-1])
        if exercise_every is not None and 0 < n < n_tree_steps and n % exercise_every == 0:
            value = np.maximum(value, intrinsic(n))
    return float(value[0])
