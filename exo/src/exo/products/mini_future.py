"""Mini future (long or short): open-ended, daily-accruing financing, stop-loss reset, continuous
knock-out, residual-value settlement -- a term sheet plus the `Payoff` it defines.

A long mini future is the underlying less a financing level `F_t = F0 * exp(rate_F * t)`; the
holder receives `S - F` at the horizon, unless the spot touches the stop-loss level `L` first, in
which case the position is stopped out and settles at the residual value `L - F` (short: signs
flipped). `rate_F = r + financing_spread` (long) / `r - financing_spread` (short), where `r` is
the DISCOUNT rate threaded through `Payoff.cashflows(bundle, *, r)` (user decision, P2.M2 slice
2): the financing base rate is the discount rate, not a term-sheet field.

`horizon` is a CARRY horizon, not a maturity: a real mini future is open-ended, and the engine
needs a finite date to simulate to and discount over. `expiry` returns it so `price()`'s
exact-expiry guard is unchanged. With `financing_spread = 0` and `q = 0` the PV is
`ratio * (S0 - F0)` for any horizon over which the reset schedule keeps the level beyond F,
and any continuous model (G-MF2).

The stop-loss resets at each of `reset_times` to `F(t_k) * (1 + buffer)` (long) /
`F(t_k) * (1 - buffer)` (short) and stays there until the next reset. `L_i` below is the level in
force on simulation step `(t_i, t_{i+1}]`; a reset at grid index `j` applies from step `j` on.

WHY A LEDGER OF EXPECTED RESIDUALS, NOT A PER-PATH STOP-OUT DATE: the ledger's `t` is
path-independent (ponytail P2-6, `base.py`). The stop-out is therefore Rao-Blackwellized onto
the simulation grid (ADR-006 Amendment 5 s1): with `A_i = prod_{j<i} q_j` the probability of
having survived to `t_i` given the path (`q` = `base.bridge_step_survival`), column `c` carries
the expected residual mass stopping at that date, so no per-path date is needed and no Python
loop over paths runs. Mass stopping

- on a RESET JUMP (the spot at `t_i` is already beyond the NEW level `L_i`): `A_i * (S_i - F_i)`
  at `t_i`, i.e. column `i - 1`;
- INSIDE step `i` (live at `t_i`, but an endpoint or the bridge crosses): residual `L_i - F_{i+1}`
  at `t_{i+1}`, i.e. column `i`;
- survivors: `A_k * (S_T - F_T)` at the horizon; all times `ratio * side_sign`.

No residual is floored at zero, and none needs to be: `cashflows()` raises unless the level in
force on every step stays strictly beyond the accrued financing level `F_{i+1}`. The model is a
diffusion with no intra-step gaps, so an in-step stop pays the deterministic `L_i - F_{i+1}`, and
a reset-jump path was alive against `L_{i-1}` at `t_i` (so `S_i` is beyond `L_{i-1}`, which is
beyond `F_i`). Every residual is then provably non-negative (in the long sign; flipped for short).
A negative residual arises only when `F` has overtaken the level in force -- the guard's regime.

# ponytail: no dividend pass-through into F (real issuers lower the financing level on ex-div).
# Ceiling: with q > 0 the PV carries the holder's dividend leakage unhedged by F. P2.M4 does not
# load mini futures (a static term sheet cannot age the accruing financing level, so theta is
# undefined; ADR-006 Amendment 9 s7). Trigger: the first mini future in the persisted book.
# Owner undecided. See docs/PONYTAIL-DEBT.md.
# ponytail: no settlement lag (the residual is paid at the stop-out date). Ceiling: ignores the
# few days a real issuer takes to unwind the hedge. Trigger: first real term sheet.
# ponytail: stop-out is dated at the END of the grid step it falls in, not at the crossing time
# (P2-6: ledger dates are path-independent). Ceiling: PV bias <= ratio * |level| * r * dt / 2 *
# P(hit) (the residual is discounted over up to one extra step). Trigger: first off-grid
# payment date, when the ledger gains per-path dates.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from exo.models.heston import PathBundle
from exo.products.base import (
    CashflowLedger,
    bridge_step_survival,
    observation_indices,
    validate_observation_schedule,
)

_SIDES = ("long", "short")

# Declared ceiling for `MiniFuture.cashflows`' peak memory, in multiples of ONE
# `(n_paths, n_steps + 1)` float64 array: tracemalloc-MEASURED against the real call at
# n_paths=20_000, n_steps=252 (warm-up call outside the window) at 8.22, identical to 3 digits at
# 40_000 x 126 and with/without resets; committed with a <5% margin. Of that, ~1.0 is the
# returned ledger itself. The ratio EXCLUDES the caller-owned input bundle (S, v), allocated
# outside the measured window: the real footprint is about bundle + 8.6 arrays.
# `test_mini_future.py`'s memory test measures again and asserts against THIS constant (and that
# it is not stale-loose), never against a re-derived array count.
_MINI_FUTURE_PEAK_ARRAY_RATIO = 8.6


@dataclass(frozen=True)
class MiniFuture:
    """Mini future term sheet. See the module docstring for the settlement mechanics.

    Long: `stop_loss_level > financing_level` (stopped out when the spot FALLS to the level);
    short: `stop_loss_level < financing_level` (stopped out when it RISES). `reset_times` may be
    empty; otherwise it must be a valid schedule within `(0, horizon]`. The term sheet has no
    `s0`, so inception breach (spot already beyond the stop-loss) is checked in `cashflows()`.
    """

    underlying: str
    side: Literal["long", "short"]
    ratio: float
    financing_level: float
    financing_spread: float
    stop_loss_level: float
    stop_loss_buffer: float
    reset_times: tuple[float, ...]
    horizon: float

    def __post_init__(self) -> None:
        # `side` feeds two-way branches, so a typo would silently price the other side.
        if self.side not in _SIDES:
            raise ValueError(f"side must be one of {_SIDES}, got {self.side!r}")
        # NaN/Inf bypass every `<=`/`<`/`>` guard under IEEE-754 -- checked explicitly.
        if not math.isfinite(self.ratio) or self.ratio <= 0.0:
            raise ValueError(f"ratio must be a positive finite number, got {self.ratio}")
        if not math.isfinite(self.financing_level) or self.financing_level <= 0.0:
            raise ValueError(
                f"financing_level must be a positive finite number, got {self.financing_level}"
            )
        if not math.isfinite(self.financing_spread):
            raise ValueError(f"financing_spread must be finite, got {self.financing_spread}")
        if not math.isfinite(self.stop_loss_level) or self.stop_loss_level <= 0.0:
            raise ValueError(
                f"stop_loss_level must be a positive finite number, got {self.stop_loss_level}"
            )
        if self.side == "long" and self.stop_loss_level <= self.financing_level:
            raise ValueError(
                f"a long mini future needs stop_loss_level ({self.stop_loss_level}) above "
                f"financing_level ({self.financing_level})"
            )
        if self.side == "short" and self.stop_loss_level >= self.financing_level:
            raise ValueError(
                f"a short mini future needs stop_loss_level ({self.stop_loss_level}) below "
                f"financing_level ({self.financing_level})"
            )
        if (
            not math.isfinite(self.stop_loss_buffer)
            or self.stop_loss_buffer <= 0.0
            or self.stop_loss_buffer >= 1.0
        ):
            raise ValueError(
                f"stop_loss_buffer must lie strictly within (0, 1), got {self.stop_loss_buffer}"
            )
        if not math.isfinite(self.horizon) or self.horizon <= 0.0:
            raise ValueError(f"horizon must be a positive finite number, got {self.horizon}")
        if len(self.reset_times) > 0:
            validate_observation_schedule(self.reset_times, self.horizon)

    @property
    def expiry(self) -> float:
        """The carry horizon (see the module docstring), under the `Payoff` Protocol's name."""
        return self.horizon

    def cashflows(self, bundle: PathBundle, *, r: float) -> CashflowLedger:
        if not math.isfinite(r):
            raise ValueError(f"r must be finite, got {r}")
        long = self.side == "long"
        sg = 1.0 if long else -1.0
        # Bound to THIS product's own horizon, not the bundle's last column (a shared bundle may
        # run past it) -- same discipline as `barrier.py`.
        k = int(observation_indices(np.array([self.horizon]), bundle)[0])
        s = bundle.S
        t = bundle.t[: k + 1]
        fin = self.financing_level * np.exp((r + sg * self.financing_spread) * t)  # F_i, (k+1,)

        # Level in force on each step: B0 until the first reset, then F(t_j) * (1 +/- buffer)
        # from reset index j on. A reset at the horizon index has no step after it.
        levels: NDArray[np.float64] = np.full(k, self.stop_loss_level)
        if len(self.reset_times) > 0:
            reset_idx = observation_indices(np.asarray(self.reset_times, dtype=np.float64), bundle)
            reset_idx = reset_idx[reset_idx < k]
            if reset_idx.size > 0:
                reset_levels = fin[reset_idx] * (1.0 + sg * self.stop_loss_buffer)
                n_resets = np.searchsorted(reset_idx, np.arange(k), side="right")
                levels = np.where(
                    n_resets == 0, self.stop_loss_level, reset_levels[np.maximum(n_resets - 1, 0)]
                )

        spot0 = s[:, 0]
        if np.any(spot0 <= levels[0]) if long else np.any(spot0 >= levels[0]):
            raise ValueError(
                f"stop-loss level {levels[0]} is already breached at inception (spot "
                f"{float(spot0.min())}..{float(spot0.max())}, side={self.side!r})"
            )

        # The level in force on step i must stay strictly beyond the accrued financing level
        # F_{i+1} (the in-step residual `L_i - F_{i+1}` is then > 0). That also covers the
        # reset-jump residual `S_i - F_i`: the path was alive against L_{i-1} at t_i, so
        # sg * (S_i - F_i) > sg * (L_{i-1} - F_i) > 0, the step i-1 case of this same check.
        if np.any(sg * (levels - fin[1:]) <= 0.0):
            raise ValueError(
                "stop-loss level in force on step i is not beyond the accrued financing level "
                "F_{i+1}; add resets or check the spread"
            )

        direction: Literal["down", "up"] = "down" if long else "up"
        q = bridge_step_survival(bundle, levels, direction, k)  # (n_paths, k)
        s_a = s[:, :k]
        live_a = s_a > levels if long else s_a < levels  # S_i live vs the level in force at i

        # a_prev[:, i] = A_i = P(alive at t_i | path); a_end = A_k.
        a_prev = np.ones_like(q)
        survived = np.cumprod(q, axis=1)
        a_prev[:, 1:] = survived[:, :-1]
        a_end = survived[:, -1]

        # Inside step i: A_i * (live_a - q) stops there, paid L_i - F_{i+1} at t_{i+1}.
        amounts = a_prev * (live_a - q)
        amounts *= levels - fin[1:]
        # Reset jump at i: A_i * (1 - live_a) stops at t_i, paid S_i - F_i -> column i - 1.
        # (Step 0 cannot jump: inception breach raised above.)
        amounts[:, :-1] += a_prev[:, 1:] * (~live_a[:, 1:]) * (s[:, 1:k] - fin[1:k])
        amounts[:, -1] += a_end * (s[:, k] - fin[k])
        amounts *= self.ratio * sg
        return CashflowLedger(t=bundle.t[1 : k + 1].copy(), amounts=amounts)
