"""Per-book aggregation: unit scaling, q labelling, hedge targets and the band trigger.

Pure. This is the ONE place where raw per-unit derivatives (`greeks/bump.py`) become desk-position
values in ADR-014 s9 units, and where the `q` bump is labelled by instrument class (ADR-006
Amendment 9 s4). Floats in, floats out: the fixed-point conversion belongs to `bus/convert.py`.

Class grouping (one mapping for bands AND q labelling, so they cannot disagree):
EQUITY / INDEX / ETF are "equity-like" (band_equity_qty_e2; q is a dividend yield -> `div_sens`);
FX_SPOT is "fx" (band_fx_qty_e2; q := r_f is a foreign rate -> `rho_foreign`). Any other class is
an error: there is no band and no label for it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from exo.bus.convert import to_lot_e2
from exo.config import RehedgeConfig
from exo.greeks.bump import Estimate, Greeks
from exo.portfolio.book import BookProduct
from exo.refdata import EQUITY_LIKE_CLASSES as _EQUITY_LIKE
from exo.refdata import FX_CLASSES as _FX
from exo.refdata import Instrument


@dataclass(frozen=True)
class LineValues:
    """One product line's DESK-POSITION values (`desk_quantity x` per-unit), in ADR-014 s9 units
    as floats: delta in units of the underlying, gamma in units per +1% spot, vega in ccy per vol
    point, rho and rho_foreign in ccy per bp, div_sens in ccy per +1% yield, theta in ccy per
    calendar day. Standard errors are non-negative and scale with `|desk_quantity|`."""

    product_id: str
    product_type: str
    instrument: Instrument
    pv: Estimate
    delta: Estimate
    gamma: Estimate
    vega: Estimate
    rho: Estimate
    div_sens: Estimate
    rho_foreign: Estimate
    theta: Estimate
    flagged: tuple[str, ...]


def flagged(name: str, est: Estimate, max_rel: float) -> bool:
    """True when `se > max_rel * |value|`; a zero value is always flagged (exo/CLAUDE.md rule 6).

    ponytail: every storyline Greek is flagged at 20,000 paths (measured: delta SE 5-8% against
    `max_rel_std_error` 2%; ADR-006 Amendment 9 s7, `tests/test_book_storyline_delta.py`), so the
    flag carries no information on the shipped book. Ceiling: delta needs ~14x the paths
    ((7.5/2)^2) to reach 2%. Upgrade: RQMC (trigger: the first published RQMC number) or a larger
    path budget once revaluation is fast enough.
    """
    if not (math.isfinite(est.value) and math.isfinite(est.std_err)):
        raise ValueError(f"{name}: non-finite estimate {est}")
    return est.value == 0.0 or est.std_err > max_rel * abs(est.value)


def _scaled(est: Estimate, k: float) -> Estimate:
    return Estimate(value=k * est.value, std_err=abs(k) * est.std_err)


def line_values(
    product: BookProduct, greeks: Greeks, *, spot: float, max_rel_std_error: float
) -> LineValues:
    """Scale one product's raw `Greeks` to a desk-signed `LineValues`.

    `spot` is the mark the Greeks were computed at (gamma is reported per +1% of it). The q bump
    is labelled by the underlying's class; the inapplicable label is exactly 0 and is NOT flagged
    (it is not a Greek of this line), so `flagged` holds at most 6 names.
    """
    instrument = product.instrument
    cls = instrument.instrument_class
    if cls not in _EQUITY_LIKE | _FX:
        raise ValueError(
            f"{instrument.symbol}: class {cls} has no q label (EQUITY/INDEX/ETF -> div_sens, "
            "FX_SPOT -> rho_foreign)"
        )
    q = product.desk_quantity
    zero = Estimate(0.0, 0.0)
    equity = cls in _EQUITY_LIKE
    values = {
        "delta": _scaled(greeks.delta, q),
        "gamma": _scaled(greeks.gamma, q * 0.01 * spot),
        "vega": _scaled(greeks.vega, q * 0.01),
        "rho": _scaled(greeks.rho, q * 1e-4),
        "theta": _scaled(greeks.theta, q),
        "div_sens": _scaled(greeks.dq, q * 0.01) if equity else zero,
        "rho_foreign": zero if equity else _scaled(greeks.dq, q * 1e-4),
    }
    names = ("delta", "gamma", "vega", "rho", "theta", "div_sens" if equity else "rho_foreign")
    return LineValues(
        product_id=product.product_id,
        product_type=product.product_type,
        instrument=instrument,
        pv=_scaled(greeks.pv, q),
        delta=values["delta"],
        gamma=values["gamma"],
        vega=values["vega"],
        rho=values["rho"],
        div_sens=values["div_sens"],
        rho_foreign=values["rho_foreign"],
        theta=values["theta"],
        flagged=tuple(n for n in names if flagged(n, values[n], max_rel_std_error)),
    )


def band_for(instrument: Instrument, rehedge: RehedgeConfig) -> int:
    """The no-trade band (qty_e2) for this instrument's class (ADR-014 s9)."""
    if instrument.instrument_class in _EQUITY_LIKE:
        return rehedge.band_equity_qty_e2
    if instrument.instrument_class in _FX:
        return rehedge.band_fx_qty_e2
    raise ValueError(f"{instrument.symbol}: class {instrument.instrument_class} has no band")


def hedge_targets(lines: Sequence[LineValues]) -> dict[int, int]:
    """`instrument_id -> target_qty_e2 = -sum(desk delta)`, rounded ONCE half-even to the lot.

    Delta is summed per underlying across the book's lines in float units and rounded a single
    time (rounding per line would let rounding error accumulate across lines).
    """
    delta_units: dict[int, float] = {}
    by_id: dict[int, Instrument] = {}
    for ln in lines:
        iid = ln.instrument.instrument_id
        delta_units[iid] = delta_units.get(iid, 0.0) + ln.delta.value
        by_id[iid] = ln.instrument
    return {iid: to_lot_e2(-d, by_id[iid].lot_e2) for iid, d in delta_units.items()}


def should_publish(new: int, last: int | None, band: int, *, unhalted: bool) -> bool:
    """Republish rule (ADR-014 s9): nothing published yet, or `|new - last| > band`, or an
    un-halt just happened."""
    return unhalted or last is None or abs(new - last) > band
