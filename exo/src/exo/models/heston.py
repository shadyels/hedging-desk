"""Heston path simulation: PathBundle + simulate().

RETAINS THE FULL PATH MATRIX. This is deliberate, not an oversight: this engine
does not accumulate a discounted payoff and discard paths as it goes, even though
that is the natural design for the barrier/autocallable payoffs P2.M1 actually
prices. Reason (exo/CLAUDE.md engine constraint 1, ADR-006 Amendment 3): P2.M2's
Longstaff-Schwartz early-exercise engine regresses the continuation value
BACKWARDS over retained `(S, v)` state at every exercise date. A streaming engine
built now would make that unimplementable in M2 without rewriting this module,
which the M2 payoff-abstraction rule forbids.

ponytail: retaining the full (n_paths, n_steps+1) `S` and `v` matrices as float64 costs ~810 MB
at 200k paths x 252 steps (two such matrices) -- BUT peak memory is actually ~1.62 GB, not 810
MB (corrected 2026-09-06, code review P0-1): `simulate()` also allocates the full (n_paths,
n_steps) DRAW matrices up front (QE: `u`, `z`; euler-ft: `z_variance`, `z_spot`), two more
float64 arrays of essentially the same size (~806 MB at the same path/step count), alongside
`S`/`v`. Ceiling: ~1.62 GB at 200k paths x 252 steps (four arrays total). Trigger: P2.M2's LSM
work, or any run that needs more paths/steps than fit in memory at once — retain only a
declared observation grid, or batch paths (see `PriceResult.combine()` in estimator.py, which
exists for exactly this; `studies/scheme_convergence.py`'s `resolve_batch_plan` is the first
consumer and accounts for all four arrays).

Two schemes are implemented:

- "qe": Andersen (2008) Quadratic-Exponential, central discretization
  (gamma1 = gamma2 = 1/2), WITH the branch-dependent martingale correction to K0
  (see `_qe_k0_star` below). QE keeps v >= 0 by construction; both branches are
  computed on the full array and selected with `np.where` — no per-path branching.
- "euler-ft": full-truncation Euler (Lord, Kahl & Jackel 2010). The variance STATE
  may go negative and is stored as-is; only its USE in the drift/diffusion is
  truncated to `max(v, 0)`. Clipping the stored state would be a different,
  higher-bias scheme.

The only Python loop in this module is over time steps — every step operates on
the full path vector at once.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from scipy.special import ndtri  # type: ignore[import-untyped]

from exo.models.params import EngineConfig, HestonParams
from exo.models.rng import RandomSource

_PSI_C = 1.5
_GAMMA1 = 0.5
_GAMMA2 = 0.5


@dataclass(frozen=True)
class PathBundle:
    """Simulated (S, v) paths plus the antithetic pairing structure they carry.

    `n_pairs` travels WITH the paths (rather than being re-derived by callers)
    because the pairing structure — which rows are original vs. mirrored — is
    needed downstream by `mc_estimate`'s pair-mean standard error and must never
    be reconstructed from `n_paths // 2` alone (that would silently be wrong for a
    non-antithetic bundle).

    `qe_fallback_count` is a diagnostic added alongside the bundle by the QE
    scheme's martingale correction (see `_qe_k0_star`): the number of (path, step)
    cells where the correction was inadmissible and fell back to the uncorrected
    K0. It is `None` for "euler-ft", which has no such correction to fall back
    from.
    """

    t: NDArray[np.float64]
    S: NDArray[np.float64]
    v: NDArray[np.float64]
    antithetic: bool
    n_pairs: int | None
    qe_fallback_count: int | None


def simulate(params: HestonParams, engine: EngineConfig, rng: RandomSource) -> PathBundle:
    """Simulate Heston (S, v) paths under `engine.scheme`.

    Requires `engine.antithetic == rng.antithetic` (P1-5, code review 2026-09-06): if `rng`
    mirrors draws but the returned bundle is tagged `antithetic=False` (or vice versa),
    `mc_estimate` silently picks the wrong standard-error formula -- in the dangerous direction,
    the naive (too-loose) one -- with no exception anywhere. Raising here instead makes every
    3-SE gate strictly HARDER to pass by mistake, never easier.
    """
    if engine.antithetic != rng.antithetic:
        raise ValueError(
            f"EngineConfig.antithetic={engine.antithetic} but RandomSource.antithetic="
            f"{rng.antithetic} -- these must match. A mismatch silently changes which "
            "standard-error formula mc_estimate uses (pair-mean vs naive) without changing "
            "what was actually drawn, which can only make a 3-SE gate falsely pass."
        )

    n_paths = engine.n_paths
    n_steps = engine.n_steps
    dt = engine.expiry / n_steps

    t = np.linspace(0.0, engine.expiry, n_steps + 1, dtype=np.float64)
    spot = np.empty((n_paths, n_steps + 1), dtype=np.float64)
    variance = np.empty((n_paths, n_steps + 1), dtype=np.float64)
    spot[:, 0] = params.s0
    variance[:, 0] = params.v0
    x: NDArray[np.float64] = np.full(n_paths, np.log(params.s0), dtype=np.float64)
    v_prev: NDArray[np.float64] = variance[:, 0].copy()

    qe_fallback_count: int | None = None

    if engine.scheme == "qe":
        qe_fallback_count = 0
        u = rng.uniforms((n_paths, n_steps), stream="variance")
        z = rng.normals((n_paths, n_steps), stream="spot")
        k1, k2, k3, k4, a_coef, uncorrected_k0 = _qe_constants(params, dt)
        for step in range(n_steps):
            x, v_next, fallback = _qe_step(
                params,
                dt,
                v_prev,
                x,
                u[:, step],
                z[:, step],
                k1,
                k2,
                k3,
                k4,
                a_coef,
                uncorrected_k0,
            )
            qe_fallback_count += fallback
            variance[:, step + 1] = v_next
            spot[:, step + 1] = np.exp(x)
            v_prev = v_next
    elif engine.scheme == "euler-ft":
        z_variance = rng.normals((n_paths, n_steps), stream="variance")
        z_spot = rng.normals((n_paths, n_steps), stream="spot")
        for step in range(n_steps):
            x, v_next = _euler_ft_step(params, dt, v_prev, x, z_variance[:, step], z_spot[:, step])
            variance[:, step + 1] = v_next
            spot[:, step + 1] = np.exp(x)
            v_prev = v_next
    else:  # pragma: no cover - EngineConfig.scheme is a validated Literal
        raise ValueError(f"unknown scheme: {engine.scheme}")

    n_pairs = n_paths // 2 if engine.antithetic else None
    return PathBundle(
        t=t,
        S=spot,
        v=variance,
        antithetic=engine.antithetic,
        n_pairs=n_pairs,
        qe_fallback_count=qe_fallback_count,
    )


def _qe_constants(
    params: HestonParams, dt: float
) -> tuple[float, float, float, float, float, float]:
    """K1..K4, `A = K2 + K4/2`, and the plain (uncorrected) K0 — all scalars,
    constant across paths and steps since dt is fixed on a uniform grid."""
    kappa, rho, xi, theta = params.kappa, params.rho, params.xi, params.theta
    k1 = _GAMMA1 * dt * (kappa * rho / xi - 0.5) - rho / xi
    k2 = _GAMMA2 * dt * (kappa * rho / xi - 0.5) + rho / xi
    k3 = _GAMMA1 * dt * (1.0 - rho**2)
    k4 = _GAMMA2 * dt * (1.0 - rho**2)
    a_coef = k2 + 0.5 * k4
    uncorrected_k0 = -rho * kappa * theta * dt / xi
    return k1, k2, k3, k4, a_coef, uncorrected_k0


def _qe_step(
    params: HestonParams,
    dt: float,
    v_prev: NDArray[np.float64],
    x_prev: NDArray[np.float64],
    u_col: NDArray[np.float64],
    z_col: NDArray[np.float64],
    k1: float,
    k2: float,
    k3: float,
    k4: float,
    a_coef: float,
    uncorrected_k0: float,
) -> tuple[NDArray[np.float64], NDArray[np.float64], int]:
    kappa, theta, xi = params.kappa, params.theta, params.xi
    ekt = np.exp(-kappa * dt)

    m = theta + (v_prev - theta) * ekt
    s2 = v_prev * xi**2 * ekt * (1.0 - ekt) / kappa + theta * xi**2 * (1.0 - ekt) ** 2 / (
        2.0 * kappa
    )
    psi = s2 / m**2
    low_branch = psi <= _PSI_C

    # ponytail (P0-3, code review 2026-09-06 -- Amendment A1's required marker, previously
    # missing): a cell where `quad_denom <= 0` (quadratic branch, A >= 1/(2a)) or `exp_denom <= 0`
    # (exponential branch, A >= beta) is INADMISSIBLE for the martingale correction and falls
    # back to the UNCORRECTED `uncorrected_k0` below, per Amendment A1 -- counted via
    # `fallback_count`/`PathBundle.qe_fallback_count`, not repaired. Ceiling: `E[S]` is only
    # APPROXIMATELY a martingale on fallback cells; the fallback fraction is reported per sweep
    # cell as `SweepCell.qe_fallback_fraction` (studies/scheme_convergence.py) precisely so this
    # is a measured, not assumed, quantity. Trigger: fallback fraction exceeding ~1% on a real
    # (non-illustrative) parameter set, at which point sub-step those cells (finer local dt)
    # instead of accepting the uncorrected K0.
    # --- quadratic branch (used where psi <= psi_c) ---
    inv_psi = 2.0 / psi
    b2 = np.maximum(
        inv_psi - 1.0 + np.sqrt(np.maximum(inv_psi, 0.0)) * np.sqrt(np.maximum(inv_psi - 1.0, 0.0)),
        0.0,
    )
    b = np.sqrt(b2)
    a = m / (1.0 + b2)
    z_v = ndtri(u_col)
    v_low = a * (b + z_v) ** 2

    quad_denom = 1.0 - 2.0 * a_coef * a  # admissible iff > 0 (A < 1/(2a))
    quad_admissible = quad_denom > 0.0
    safe_quad_denom = np.where(quad_admissible, quad_denom, 1.0)
    k0_star_quad = np.where(
        quad_admissible,
        -a_coef * a * b2 / safe_quad_denom
        + 0.5 * np.log(safe_quad_denom)
        - (k1 + 0.5 * k3) * v_prev,
        uncorrected_k0,
    )

    # --- exponential branch (used where psi > psi_c) ---
    p = (psi - 1.0) / (psi + 1.0)
    beta = (1.0 - p) / m
    with np.errstate(divide="ignore", invalid="ignore"):
        exp_inverse = np.log(np.maximum((1.0 - p) / (1.0 - u_col), 1e-300)) / beta
        v_high = np.where(u_col <= p, 0.0, exp_inverse)

    exp_denom = beta - a_coef  # admissible iff > 0 (A < beta)
    exp_admissible = exp_denom > 0.0
    safe_exp_denom = np.where(exp_admissible, exp_denom, 1.0)
    mgf = p + beta * (1.0 - p) / safe_exp_denom
    k0_star_exp = np.where(
        exp_admissible,
        -np.log(np.maximum(mgf, 1e-300)) - (k1 + 0.5 * k3) * v_prev,
        uncorrected_k0,
    )

    v_next = np.where(low_branch, v_low, v_high)
    k0_star = np.where(low_branch, k0_star_quad, k0_star_exp)
    fallback_mask = np.where(low_branch, ~quad_admissible, ~exp_admissible)
    fallback_count = int(np.count_nonzero(fallback_mask))

    r, q = params.r, params.q
    diffusion = np.sqrt(np.maximum(k3 * v_prev + k4 * v_next, 0.0))
    x_next = x_prev + (r - q) * dt + k0_star + k1 * v_prev + k2 * v_next + diffusion * z_col
    return x_next, v_next, fallback_count


def _euler_ft_step(
    params: HestonParams,
    dt: float,
    v_prev: NDArray[np.float64],
    x_prev: NDArray[np.float64],
    z_variance: NDArray[np.float64],
    z_perp: NDArray[np.float64],
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    kappa, theta, xi, rho = params.kappa, params.theta, params.xi, params.rho
    r, q = params.r, params.q

    v_plus = np.maximum(v_prev, 0.0)
    z_spot = rho * z_variance + np.sqrt(1.0 - rho**2) * z_perp

    x_next = x_prev + (r - q - 0.5 * v_plus) * dt + np.sqrt(v_plus * dt) * z_spot
    v_next = v_prev + kappa * (theta - v_plus) * dt + xi * np.sqrt(v_plus * dt) * z_variance
    return x_next, v_next
