"""The payoff abstraction: an undiscounted, dated cashflow ledger.

Slice 1 shipped no `Payoff` abstraction -- every gate test inlined its payoff, deliberately,
because an abstraction designed against zero real products is an abstraction designed against
nothing (ADR-006 Amendment 4 s3). This module and the two products built against it in the same
pass (`products/barrier.py`, `products/autocallable.py`) are that abstraction's first real load.

WHY UNDISCOUNTED AND DATED, NOT A DISCOUNTED SCALAR PER PATH: early redemption (autocallable),
coupon strips, TARF accumulation (P2.M3) and mini-future residual-value settlement on stop-out
(P2.M2) all become "a cashflow on a different date" under this shape rather than four bespoke
payoff mechanisms. Discounting is centralized in `products/pricer.py` and tested once there.
Critically, keeping `r` OUT of the payoff means a payoff's shape does not change when P2.M4 bumps
`r` for rho -- a payoff that discounted internally would need its own bump-awareness. CAVEAT
(fix round 1, code review): this does not survive P2.M2's LSM American exercise unchanged --
Longstaff-Schwartz must discount *inside* the backward induction to compare continuation value
against intrinsic value at each exercise date, so "r stays out of the payoff" is a property of
this ledger shape, not an absolute invariant every future payoff must honour. SECOND CAVEAT
(fix round 3, architect review, reservation (e)): it also does not survive P2.M2's mini future,
for a different reason than LSM's. ADR-006 Amendment 3 defines a mini future as a
continuously-monitored knock-out PLUS a daily financing accrual, and financing accrual is
rate-driven -- the payoff will need a rate (risk-free plus issuer spread) as an input, and under
a P2.M4 rho bump that leg SHOULD move. That is genuine rho, not the artifact this rationale
warns against (a payoff that discounts INTERNALLY and so needs its own bump-awareness just to
stay consistent with an external rate it never sees again). The M2 implementer has two available
shapes for this: a rate field on the mini-future term sheet (which P2.M4's bump loop must then
know to bump, same as any other term-sheet field), or an `r` argument threaded through
`cashflows` (which touches every payoff's signature, not just the mini future's). BOTH shapes
are `products/`-local changes -- neither requires touching `models/` -- so neither violates
`exo/CLAUDE.md`'s M2 rule, despite reading, out of context, like exactly the kind of change that
rule exists to catch. Spelling this out here so an M2 reader who hits the financing leg does not
conclude this abstraction is broken and re-cut it unnecessarily.

The `Payoff` Protocol takes a single `PathBundle`. `exo/CLAUDE.md`'s M2 abstraction rule: if a new
payoff requires touching `models/`, the abstraction is wrong. Every payoff-specific mechanism
here -- the observation-to-grid-index mapping, the Brownian-bridge survival weight -- lives in
THIS module (a function of a `PathBundle` that a product's `cashflows()` calls), not in
`models/`, precisely so new products stay a `products/` change.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

import numpy as np
from numpy.typing import NDArray

from exo.models.heston import PathBundle


class Monitoring(StrEnum):
    """How a barrier (or any other level-crossing condition) is monitored against a
    `PathBundle`'s simulated steps.

    `CONTINUOUS_BRIDGE` uses a Brownian-bridge per-step survival probability (see
    `barrier_survival`) -- what P2.M2's mini future needs (continuous knock-out).
    `DISCRETE` checks a declared observation schedule with a hard indicator -- what
    P2.M3's TARF needs (monthly fixings). Building both now, each exercised by a real
    product (`products/barrier.py`), is the minimal spanning set (brief s2).
    """

    CONTINUOUS_BRIDGE = "continuous_bridge"
    DISCRETE = "discrete"


# Shared barrier-direction vocabulary (R2-4, fix round 2, code review): the ONE copy for
# `products/` -- `barrier.py` imports this rather than re-declaring it, since `base.py` and
# `barrier.py` are one package and there is no reason for two copies to be able to drift.
# `analytic.py`'s OWN copy is deliberately NOT unified with this one: `models/` importing from
# `products/` would invert the dependency direction `exo/CLAUDE.md`'s M2 rule exists to protect
# (payoffs may depend on models, models must never depend on payoffs) -- see `analytic.py`'s own
# comment at its copy.
_DIRECTIONS = ("down", "up")


class ScheduleAlignmentError(ValueError):
    """An observation schedule does not land on the `PathBundle`'s simulation grid.

    Silently snapping an observation to the nearest step changes the price, so misalignment is
    a hard error here rather than a fixup -- matching this repo's posture (e.g. `cross_px_policy`
    / `[tracker]`) of failing loud on configuration that cannot be honoured.
    """


@dataclass(frozen=True)
class CashflowLedger:
    """Per-path, per-observation cashflows in product currency, UNDISCOUNTED.

    `t`: payment times in years, ascending, shape `(n_obs,)`.
    `amounts`: shape `(n_paths, n_obs)`. Row order MUST match the `PathBundle` the payoff was
    computed against -- `estimator.py`'s antithetic pair-mean standard error is POSITIONAL
    (row i is the antithetic twin of row i + n_pairs), so a ledger that reorders rows silently
    mispairs it.

    # ponytail (P2-6, P2.M1 slice 2 fix round 1, code review): `t` is PATH-INDEPENDENT --
    # shared across every path -- so no payoff built on this ledger can emit a cashflow whose
    # DATE varies by path. Ceiling: cannot represent a path-dependent payment date. Trigger:
    # P2.M2's mini-future residual-value settlement on stop-out (each path stops out on its own
    # date) and, separately, P2.M2's LSM American exercise (each path exercises on its own
    # date). Escape: widen `amounts` to `(n_paths, n_steps+1)` against the FULL simulation grid
    # and emit one non-zero column per path -- survivable without re-cutting this abstraction,
    # but a real cost: a third `(n_paths, n_steps+1)` float64 matrix alongside `heston.py`'s
    # already-measured `S`/`v` pair (see that module's own memory ponytail for the ceiling this
    # interacts with).
    """

    t: NDArray[np.float64]
    amounts: NDArray[np.float64]

    def __post_init__(self) -> None:
        if self.t.ndim != 1:
            raise ValueError(f"t must be 1-D (n_obs,), got shape {self.t.shape}")
        if self.amounts.ndim != 2:
            raise ValueError(
                f"amounts must be 2-D (n_paths, n_obs), got shape {self.amounts.shape}"
            )
        if self.amounts.shape[1] != self.t.shape[0]:
            raise ValueError(
                f"amounts.shape[1] ({self.amounts.shape[1]}) must equal t.shape[0] "
                f"({self.t.shape[0]}) -- one column per payment date"
            )
        if self.t.shape[0] > 1 and np.any(np.diff(self.t) <= 0.0):
            raise ValueError(f"t must be strictly ascending, got {self.t.tolist()}")


class Payoff(Protocol):
    """A priceable product: something that turns one `PathBundle` into a `CashflowLedger`.

    Single-underlying by design in P2.M1 -- `cashflows` takes exactly one `PathBundle`. See the
    ponytail marker below for the P4.M5 multi-asset (worst-of) upgrade this defers.

    # ponytail (P2-3, P2.M1 slice 2): `cashflows` takes a single `PathBundle`, so a payoff can
    # only reference one underlying's simulated path. Ceiling: no multi-asset product (a
    # worst-of autocallable, a basket barrier) can be expressed against this Protocol as-is.
    # Trigger: P4.M5 multi-asset work -- likely widens the signature to a mapping of underlying
    # -> PathBundle (all sharing one PathBundle.t grid) rather than reworking every M1/M2/M3
    # single-asset product.
    """

    @property
    def expiry(self) -> float: ...

    def cashflows(self, bundle: PathBundle) -> CashflowLedger: ...


def observation_indices(times: NDArray[np.float64], bundle: PathBundle) -> NDArray[np.intp]:
    """Map observation times onto `bundle.t` grid indices.

    Raises `ScheduleAlignmentError` unless every observation lands on a grid point within
    `1e-9 * expiry` (`expiry = bundle.t[-1]`), and rejects any observation at `t == 0` (t=0 is
    the pricing date, not an observable payment/fixing date). Silently snapping to the nearest
    step changes the price, so misalignment is a hard error (see `ScheduleAlignmentError`).

    NaN/Inf are rejected explicitly (HIGH-3, security review, fix round 1) rather than left to
    the relational checks below: IEEE-754 makes every one of `<= 0.0`, `> tol` False for NaN, so
    a NaN observation would otherwise sail straight through this function's OWN alignment guard
    -- the one `ScheduleAlignmentError` exists specifically to enforce -- and get mapped to an
    arbitrary grid index instead of being rejected.
    """
    expiry = float(bundle.t[-1])
    tol = 1e-9 * expiry
    times_arr = np.asarray(times, dtype=np.float64)

    if not np.all(np.isfinite(times_arr)):
        raise ScheduleAlignmentError(
            f"observation times must all be finite, got {times_arr.tolist()}"
        )

    if np.any(times_arr <= 0.0):
        raise ScheduleAlignmentError(
            f"observation times must be > 0 (t=0 is the pricing date, not observable), got "
            f"{times_arr.tolist()}"
        )

    grid = bundle.t
    right = np.clip(np.searchsorted(grid, times_arr), 1, grid.shape[0] - 1)
    left = right - 1
    dist_right = np.abs(grid[right] - times_arr)
    dist_left = np.abs(grid[left] - times_arr)
    nearest = np.where(dist_left <= dist_right, left, right)
    nearest_dist = np.minimum(dist_left, dist_right)

    if np.any(nearest_dist > tol):
        bad = times_arr[nearest_dist > tol]
        raise ScheduleAlignmentError(
            f"observation time(s) {bad.tolist()} do not land on a PathBundle grid point "
            f"within tol={tol} (expiry={expiry}, n_steps={grid.shape[0] - 1})"
        )
    return nearest.astype(np.intp)


def validate_observation_schedule(
    observations: tuple[float, ...], expiry: float, *, require_terminal: bool = False
) -> None:
    """Validate an observation schedule shared by every path-dependent product (MEDIUM-4, code
    review, fix round 1): non-empty, all finite, strictly ascending, no duplicates, and every
    date within `(0, expiry]`.

    Extracted here -- where the schedule vocabulary already lives, alongside
    `observation_indices` -- because `barrier.py` and `autocallable.py` each carried a
    near-identical hand-rolled copy of these checks with divergent messages; P2.M2 alone adds
    six more product families that would otherwise each add a seventh and eighth copy.

    Folds the NaN/Inf guard HIGH-3 (security review, fix round 1) required: every check here
    uses `<=`/`>` comparisons, and those are False for NaN under IEEE-754, so a NaN observation
    would otherwise pass every one of them unrejected.

    `require_terminal=True` additionally requires the LAST observation to equal `expiry`
    within `1e-9 * expiry` -- the SAME tolerance `observation_indices` already uses for grid
    alignment, not exact float equality. An exact check genuinely bites on accumulated
    schedules: `sum(1 / 12 for _ in range(24))` is `1.9999999999999991 != 2.0`, a plausible way
    to build P2.M3's monthly TARF fixings (LOW-1, code review, fix round 1).

    `expiry` itself is assumed already validated (finite and positive) by the caller before
    this function is invoked -- a NaN or non-positive `expiry` would otherwise silently defeat
    the `> expiry` bound below the same way a NaN observation would.
    """
    if len(observations) == 0:
        raise ValueError("observations must be non-empty")

    obs_arr = np.asarray(observations, dtype=np.float64)
    if not np.all(np.isfinite(obs_arr)):
        raise ValueError(f"observations must all be finite, got {observations}")
    if np.any(obs_arr <= 0.0) or np.any(obs_arr > expiry):
        raise ValueError(f"observations must lie within (0, expiry={expiry}], got {observations}")
    if list(observations) != sorted(observations) or len(set(observations)) != len(observations):
        raise ValueError(f"observations must be strictly ascending, got {observations}")

    if require_terminal:
        tol = 1e-9 * expiry
        if abs(observations[-1] - expiry) > tol:
            raise ValueError(
                f"the last observation ({observations[-1]}) must equal expiry ({expiry}) "
                f"within tol={tol} -- the maturity leg is paid alongside the final "
                "observation's coupon check"
            )


# Declared ceiling for `barrier_survival`'s CONTINUOUS_BRIDGE peak memory (P2-7 ponytail below),
# tracemalloc-MEASURED at ~10.2 (fix round 1, security review MEDIUM-7). Lives here as the ONE
# place the number is written -- `test_products_base.py`'s covering test imports and asserts
# against THIS constant, not a test-local literal, so the declared ceiling and the guard cannot
# drift apart the way a comment-only figure could (fix round 2, R2-1).
_BRIDGE_PEAK_ARRAY_RATIO = 11.0


def barrier_survival(
    bundle: PathBundle,
    level: float,
    direction: Literal["down", "up"],
    monitoring: Monitoring,
    obs_idx: NDArray[np.intp],
) -> NDArray[np.float64]:
    """Per-path survival probability (never breaching `level`) over `obs_idx`, shape
    `(n_paths,)`, every entry in `[0, 1]`.

    For `DISCRETE`, survival is the hard indicator that no observation in `obs_idx` breached.

    For `CONTINUOUS_BRIDGE`, `obs_idx` must be a CONTIGUOUS run of grid indices starting at 0
    (`[0, 1, ..., k]`) -- every step from inception up to and including index `k` monitored,
    no gaps. This is deliberately NOT required to be the bundle's entire grid: a payoff with
    `expiry` shorter than the bundle's own horizon (e.g. `price_from_bundle` sharing one longer
    bundle across several products, HIGH-1 fix round 1) passes `obs_idx = arange(expiry_idx +
    1)`, a strict PREFIX of `bundle.t`. What must never happen is a gap in the middle of that
    prefix, which would silently apply the left-point variance approximation below across
    multiple simulation steps at once instead of one. Conditional on the simulated endpoints
    of a step, the probability a driftless Brownian bridge touched the barrier during that
    step is known in closed form (Glasserman, *Monte Carlo Methods in Financial Engineering*
    s6.4):

        x_i     = ln(S_i     / H)
        x_{i+1} = ln(S_{i+1} / H)
        p_cross_i = exp(-2 * x_i * x_{i+1} / (v_i * dt))   # same expression, up or down
        w_survive = prod_i (1 - p_cross_i)                 # and 0 on any breached ENDPOINT

    Three guards, all required (brief s4.1):
      - `v_i` is clamped at 0 before use ("euler-ft" permits a negative variance STATE);
      - where `v_i * dt <= 0`, `p_cross` is set to 0 (no diffusion in that step -> the bridge
        cannot cross between two un-breached endpoints);
      - a path whose ENDPOINT already breached short-circuits to survival 0 exactly.

    # ponytail (P2-1, P2.M1 slice 2): survival is a PROBABILITY, carrying no crossing TIME, so
    # a knock-out here cannot emit a dated rebate cashflow (there is no "when" to date it at).
    # Ceiling: no rebate-bearing product can be built on this function as-is. Trigger: P2.M2's
    # mini future, which settles at residual value on stop-out and needs the stop-out date --
    # likely needs an expected-first-crossing-time estimator alongside (not instead of) this
    # survival weight.
    # ponytail (P2-2, P2.M1 slice 2): the step's variance is taken at the LEFT point,
    # `v_i = max(bundle.v[:, i], 0)` -- exact under the BS-degenerate limit this module's own
    # gate (G4) validates against, but an approximation once vol is genuinely stochastic within
    # a step. Ceiling: accuracy is unverified outside the degenerate limit. Trigger: P4.M4's PDE
    # cross-check, which can quantify the approximation's bias under real Heston parameters.
    # ponytail (P2-7, P2.M1 slice 2 fix round 1, security review MEDIUM-7): CONTINUOUS_BRIDGE's
    # peak memory, `tracemalloc`-MEASURED against this real function (not hand-counted -- see
    # this repo's own heston.py memory ponytail, wrong twice before a measurement settled it, and
    # fix round 2 R2-1, which deleted an array-by-array hand-enumeration from this exact marker
    # for the same reason: it was already wrong, counting `dt`'s `(1, n_steps)` broadcast as
    # full-size). Peak/array ratio ~10.2 at both n_paths=20_000/n_steps=100 and n_paths=40_000/
    # n_steps=200 ("one array" = one `(n_paths, n_steps+1)` float64 array's worth of bytes).
    # Declared ceiling: `_BRIDGE_PEAK_ARRAY_RATIO` below (the SAME constant the covering test
    # imports and asserts against, so the declared number and the guard cannot drift apart).
    # Derived ceiling at a 20k-path, 252-step barrier price call: ~413 MB (decimal MB, from the
    # MEASURED ratio, not the rounded-up declared one); several GB at `heston.py`'s 200k x 252
    # memory ceiling if a portfolio revaluation calls this once per barrier position without
    # releasing the bundle in between. Trigger (widened, fix round 3, architect review,
    # reservation (b)): P2.M4's portfolio revaluation loop is NOT the first consumer at risk --
    # P2.M2's mini future is. A mini future is an open-ended, continuously-monitored knock-out
    # with DAILY financing accrual (ADR-006 Amendment 3), the first product that pushes
    # `n_steps` into the high hundreds on a long horizon. At n_paths=20_000, n_steps=504 (roughly
    # two years of daily steps), the SAME measured ~10.2 ratio above gives ~824 MB (decimal MB;
    # 20_000 x 505 x 8 bytes x ~10.2) for a single `barrier_survival` call -- before P2-6's third
    # matrix (see `CashflowLedger`'s own marker) lands on the same product and compounds with
    # this one rather than being independent. Fires at whichever comes first: P2.M2's mini
    # future on a daily grid, or P2.M4's portfolio revaluation. Concrete threshold, not a
    # judgment call: `n_paths * n_steps > 1.2e7` is roughly where a single call's peak crosses
    # ~1 GB at the measured ratio -- whether this needs restructuring into a step-wise
    # accumulation (trading vectorization for memory) is the architect's call, to be made
    # against this measured number, not against an estimate.
    """
    if direction not in _DIRECTIONS:
        raise ValueError(f"direction must be one of {_DIRECTIONS}, got {direction!r}")
    if not math.isfinite(level):
        # R2-2 (fix round 2, security review): HIGH-3's site list (fix round 1) missed this
        # parameter. Under DISCRETE, `obs_s <= level`/`obs_s >= level` is False everywhere for a
        # NaN level, so survival came back a silent 1.0 (never-breached) with NO signal at all --
        # strictly worse than CONTINUOUS_BRIDGE, where a NaN level at least propagates NaN
        # (loud). Unreachable from BarrierOption today (its own `barrier` field is validated),
        # but this function is publicly re-exported -- the same exposure LOW-2 already used.
        raise ValueError(f"level must be finite, got {level}")
    if monitoring == Monitoring.CONTINUOUS_BRIDGE and not np.array_equal(
        obs_idx, np.arange(obs_idx.shape[0])
    ):
        raise ValueError(
            "CONTINUOUS_BRIDGE requires obs_idx to be a contiguous run of grid indices "
            f"starting at 0 (0, 1, ..., k), got {obs_idx.tolist()} -- a gap would silently "
            "apply the left-point variance approximation across multiple simulation steps"
        )

    s = bundle.S
    v = bundle.v

    obs_s = s[:, obs_idx]
    breached = obs_s <= level if direction == "down" else obs_s >= level
    endpoint_breach = np.any(breached, axis=1)

    if monitoring == Monitoring.DISCRETE:
        return np.where(endpoint_breach, 0.0, 1.0)

    idx_a = obs_idx[:-1]
    idx_b = obs_idx[1:]
    s_a = s[:, idx_a]
    s_b = s[:, idx_b]
    v_a = np.maximum(v[:, idx_a], 0.0)
    dt = (bundle.t[idx_b] - bundle.t[idx_a])[np.newaxis, :]

    x_a = np.log(s_a / level)
    x_b = np.log(s_b / level)
    denom = v_a * dt
    diffusive = denom > 0.0
    safe_denom = np.where(diffusive, denom, 1.0)
    # A pair straddling the barrier (one endpoint breached, the other not) can drive the
    # exponent below -- and, on the same straddling pairs, the DIVISION feeding it -- to a large
    # positive value that overflows to +inf -- harmless, since such a pair always belongs to a
    # path caught by `endpoint_breach` below and its p_cross is discarded, never left to silently
    # saturate at 1.0 for a path that should survive. Airtight only because `monitoring ==
    # CONTINUOUS_BRIDGE` guarantees `obs_idx` is a contiguous, gap-free run of steps (enforced
    # above): a gap larger than one simulation step would let a genuinely-surviving path have a
    # non-overflowing but wrong p_cross instead.
    with np.errstate(over="ignore"):
        p_cross = np.where(diffusive, np.exp(-2.0 * x_a * x_b / safe_denom), 0.0)
    p_cross = np.clip(p_cross, 0.0, 1.0)

    survive_product = np.prod(1.0 - p_cross, axis=1)
    survival = np.where(endpoint_breach, 0.0, survive_product)
    return np.clip(survival, 0.0, 1.0)
