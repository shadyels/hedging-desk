"""Longstaff-Schwartz (LSM) early-exercise engine (ADR-006 Amendments 3 and 7 s2).

DESIGN, in one paragraph. The exercise POLICY is fitted on a PILOT bundle (`fit_exercise_policy`)
and then applied, without refitting, to an independent, DISJOINT valuation bundle
(`apply_exercise_policy`). Because the policy is fixed before the valuation paths exist, every
valuation path's value is the payoff of a feasible (if sub-optimal) stopping rule: the estimator
is a LOWER BOUND in expectation and the antithetic pair-mean SE of `mc_estimate` stays valid.
Fitting and valuing on the same paths would add a foresight bias that grows as the basis grows and
breaks the upper bound the G-W3 gate relies on, in principle; at the G-W3 fixture it measures
only ~0.5 SE, so `price_american`'s seed guard, not the gate, is the protection.

Regression is on the realised discounted future cashflow, ITM paths only (Longstaff-Schwartz
2001), on a quadratic basis in (S, v) -- exo/CLAUDE.md: regressing on `S` alone is a defect under
stochastic vol (gate G-W4 measures it). `np.linalg.lstsq` handles the rank-deficient case: under a
BS-degenerate model `v` is constant, so the `v`, `v^2`, `x*v` columns are collinear with the
intercept and the min-norm solution is used.

"American" here is BERMUDAN on the simulation grid: `exercise_idx` is every grid step the caller
supplies (the pricer passes every step in (0, expiry)). The gap to continuous exercise is part of
the reported low bias.

JENSEN CONTINUATION FLOOR. Exercise iff `intrinsic > max(C_hat, floor)` (strict), where `floor =
exp(-r D) * intrinsic(S * exp((r-q) D))` and D is the gap to the next exercise date. Holding one
step and exercising is feasible, and by Jensen (intrinsic convex) its value is at least `floor`,
so the true continuation is never below it. The floor removes spurious early exercise of a
zero-dividend call from a noisy regression (715-1,558 of 50,000 paths without it, measured
at the G-W2 fixtures). PRECONDITION: the
intrinsic callable must be convex in S (call/put payoffs are). This module imports nothing from
`products/`; the intrinsic is passed in as a callable.

# ponytail: the floor uses the exercise-date gap D, valid for r - q constant (flat rates). Ceiling:
# a term-structure drift would need the forward over [t, t+D]. Trigger: P4.M1 rates foundation.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from exo.models.heston import PathBundle

Basis = Callable[[NDArray[np.float64], NDArray[np.float64]], NDArray[np.float64]]
"""`(x, v) -> (m, n_basis)`, with `x = S / s_scale`."""

Intrinsic = Callable[[NDArray[np.float64]], NDArray[np.float64]]

_MIN_ITM_PER_COEFFICIENT = 10


def quadratic_sv_basis(x: NDArray[np.float64], v: NDArray[np.float64]) -> NDArray[np.float64]:
    """`[1, x, x^2, v, v^2, x*v]` as an `(m, 6)` matrix."""
    return np.column_stack([np.ones_like(x), x, x * x, v, v * v, x * v])


@dataclass(frozen=True, eq=False)
class ExercisePolicy:
    exercise_idx: NDArray[np.intp]
    """Early-exercise grid indices, ascending, excluding the terminal index."""
    terminal_idx: int
    coefficients: tuple[NDArray[np.float64] | None, ...]
    """One per `exercise_idx`; `None` means fewer than `10 * n_basis` ITM pilot paths at that date,
    so the regression was not trusted and the policy never exercises there."""
    basis: Basis
    s_scale: float
    """The pilot bundle's initial spot; the basis sees `S / s_scale`."""

    def __post_init__(self) -> None:
        if len(self.coefficients) != len(self.exercise_idx):
            raise ValueError(
                f"{len(self.coefficients)} coefficient sets for "
                f"{len(self.exercise_idx)} exercise dates"
            )


@dataclass(frozen=True, eq=False)
class ExerciseOutcome:
    discounted: NDArray[np.float64]
    """`(n_paths,)` present value at t=0 of each path's exercise payoff."""
    exercise_step: NDArray[np.intp]
    """Grid index at which each path exercised; `terminal_idx` if it held to the end."""


def _check_grid(
    bundle: PathBundle, exercise_idx: NDArray[np.intp], terminal_idx: int, r: float, q: float
) -> None:
    if bundle.low_discrepancy:
        raise ValueError(
            "LSM cannot run on a low-discrepancy (RQMC) PathBundle: regression across scrambled "
            "paths and the plain/pair-mean SE are both invalid (ADR-006 Amendment 6/7)."
        )
    if not (math.isfinite(r) and math.isfinite(q)):
        raise ValueError(f"r and q must be finite, got r={r}, q={q}")
    if not 0 < terminal_idx < bundle.S.shape[1]:
        raise ValueError(f"terminal_idx={terminal_idx} outside the bundle grid")
    if exercise_idx.size and (
        exercise_idx[0] <= 0
        or exercise_idx[-1] >= terminal_idx
        or np.any(np.diff(exercise_idx) <= 0)
    ):
        raise ValueError("exercise_idx must be strictly ascending within (0, terminal_idx)")


def _gaps(
    t: NDArray[np.float64], exercise_idx: NDArray[np.intp], terminal_idx: int
) -> NDArray[np.float64]:
    """Time from each exercise date to the next exercise date (or the terminal date)."""
    nxt = np.append(exercise_idx[1:], terminal_idx)
    gaps: NDArray[np.float64] = t[nxt] - t[exercise_idx]
    return gaps


def _exercising_rows(
    s: NDArray[np.float64],
    v: NDArray[np.float64],
    coef: NDArray[np.float64],
    *,
    basis: Basis,
    s_scale: float,
    intrinsic: Intrinsic,
    gap: float,
    r: float,
    q: float,
) -> tuple[NDArray[np.intp], NDArray[np.float64]]:
    """Rows (path indices) that exercise now, and their intrinsic values.

    ITM is required explicitly: with a negative regression estimate a zero payoff would otherwise
    "beat" the continuation and be counted as an exercise.
    """
    g = intrinsic(s)
    itm = np.flatnonzero(g > 0.0)
    if itm.size == 0:
        return itm.astype(np.intp), g[itm]
    g_itm = g[itm]
    cont = basis(s[itm] / s_scale, v[itm]) @ coef
    floor = math.exp(-r * gap) * intrinsic(s[itm] * math.exp((r - q) * gap))
    take = g_itm > np.maximum(cont, floor)
    return itm[take].astype(np.intp), g_itm[take]


def fit_exercise_policy(
    bundle: PathBundle,
    intrinsic: Intrinsic,
    *,
    exercise_idx: NDArray[np.intp],
    terminal_idx: int,
    r: float,
    q: float,
    basis: Basis = quadratic_sv_basis,
) -> ExercisePolicy:
    """Fit the exercise policy on `bundle` (the PILOT) by backward induction."""
    _check_grid(bundle, exercise_idx, terminal_idx, r, q)
    t, spot, var = bundle.t, bundle.S, bundle.v
    s_scale = float(spot[0, 0])
    n_basis = int(basis(np.ones(1), np.ones(1)).shape[1])
    gaps = _gaps(t, exercise_idx, terminal_idx)

    value = intrinsic(spot[:, terminal_idx])  # realised cashflow, valued at the date it is dated
    coefficients: list[NDArray[np.float64] | None] = [None] * len(exercise_idx)
    for j in range(len(exercise_idx) - 1, -1, -1):
        idx = int(exercise_idx[j])
        value = value * math.exp(-r * float(gaps[j]))  # now valued at t[idx]
        s, v = spot[:, idx], var[:, idx]
        itm = np.flatnonzero(intrinsic(s) > 0.0)
        if itm.size < _MIN_ITM_PER_COEFFICIENT * n_basis:
            continue
        design = basis(s[itm] / s_scale, v[itm])
        coef = np.asarray(np.linalg.lstsq(design, value[itm], rcond=None)[0], dtype=np.float64)
        coefficients[j] = coef
        rows, g_rows = _exercising_rows(
            s,
            v,
            coef,
            basis=basis,
            s_scale=s_scale,
            intrinsic=intrinsic,
            gap=float(gaps[j]),
            r=r,
            q=q,
        )
        value[rows] = g_rows
    return ExercisePolicy(
        exercise_idx=exercise_idx.copy(),
        terminal_idx=terminal_idx,
        coefficients=tuple(coefficients),
        basis=basis,
        s_scale=s_scale,
    )


def apply_exercise_policy(
    bundle: PathBundle,
    intrinsic: Intrinsic,
    policy: ExercisePolicy,
    *,
    r: float,
    q: float,
) -> ExerciseOutcome:
    """Apply the FIXED `policy` to `bundle` (the VALUATION bundle): each path exercises at the
    first date where the policy says so, else holds to `terminal_idx`. No refitting."""
    _check_grid(bundle, policy.exercise_idx, policy.terminal_idx, r, q)
    t, spot, var = bundle.t, bundle.S, bundle.v
    n_paths = spot.shape[0]
    gaps = _gaps(t, policy.exercise_idx, policy.terminal_idx)

    alive = np.ones(n_paths, dtype=bool)
    step = np.full(n_paths, policy.terminal_idx, dtype=np.intp)
    discounted = np.zeros(n_paths, dtype=np.float64)
    for j, idx_np in enumerate(policy.exercise_idx):
        coef = policy.coefficients[j]
        if coef is None:
            continue
        idx = int(idx_np)
        rows, g_rows = _exercising_rows(
            spot[:, idx],
            var[:, idx],
            coef,
            basis=policy.basis,
            s_scale=policy.s_scale,
            intrinsic=intrinsic,
            gap=float(gaps[j]),
            r=r,
            q=q,
        )
        keep = alive[rows]
        rows, g_rows = rows[keep], g_rows[keep]
        step[rows] = idx
        discounted[rows] = math.exp(-r * float(t[idx])) * g_rows
        alive[rows] = False
    held = np.flatnonzero(alive)
    terminal = policy.terminal_idx
    discounted[held] = math.exp(-r * float(t[terminal])) * intrinsic(spot[held, terminal])
    return ExerciseOutcome(discounted=discounted, exercise_step=step)
