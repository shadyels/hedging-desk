"""The float <-> fixed-point boundary, and the Protobuf builders / decoders built on it.

ROUNDING POLICY (exo/CLAUDE.md rule 3, root CLAUDE.md invariant #1). Floating point stays inside
pricing math; everything that crosses the bus is a sint64 fixed-point integer. `to_e9`, `to_e2`
and `to_lot_e2` round HALF-EVEN (banker's rounding) on the EXACT BINARY VALUE of the float -- no
decimal repr is consulted, so `0.125 -> 12` and `0.375 -> 38` at e2 (exact ties, resolved to
even), while a value like 2.675 that is a hair under its decimal tie rounds the way the stored
double says, not the way a human reading "2.675" would. The conversion is exact (`Fraction`), so
there is no double rounding. Non-finite input and anything that overflows sint64 raise
`ValueError` rather than wrap; `-0.0` maps to `0`.

`to_lot_e2` is the hedge-target rounding: ONE half-even rounding of a quantity in units to a
multiple of the instrument's `lot_e2` (not round-to-e2 then round-to-lot, which would round twice).

Outbound free text (`reason`, `text`, health detail values) is capped at 256 bytes on a UTF-8
code-point boundary (ADR-014 s14); repeated fields are capped at the ADR-014 s14 limits.
"""

from __future__ import annotations

import math
import secrets
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING

from exo.bus.gen import common_pb2, live_pb2
from exo.provenance import ValuationMetaFields
from exo.refdata import Instrument

if TYPE_CHECKING:
    from exo.portfolio.aggregate import LineValues

_I64_MIN = -(2**63)
_I64_MAX = 2**63 - 1

MAX_TEXT_BYTES = 256
MAX_SNAPSHOT_LINES = 1024
MAX_FLAGGED_GREEKS = 8
_SCHEMA_VERSION = 1
_PRODUCER = "exo"


# --- float -> fixed point -------------------------------------------------------------------


def _check_i64(n: int, x: object, what: str) -> int:
    if not _I64_MIN <= n <= _I64_MAX:
        raise ValueError(f"{what}: {x!r} overflows sint64 after scaling")
    return n


def _round_scaled(x: float, scale: Fraction, what: str) -> int:
    if not math.isfinite(x):
        raise ValueError(f"{what}: cannot convert non-finite {x!r}")
    return round(Fraction(x) * scale)  # round() on a Fraction is exact and half-even


def to_e9(x: float) -> int:
    """`x * 1e9`, half-even on the exact binary value; raises on non-finite or sint64 overflow."""
    return _check_i64(_round_scaled(x, Fraction(10**9), "to_e9"), x, "to_e9")


def to_e2(x: float) -> int:
    """`x * 1e2`, half-even on the exact binary value; raises on non-finite or sint64 overflow."""
    return _check_i64(_round_scaled(x, Fraction(100), "to_e2"), x, "to_e2")


def to_lot_e2(x_units: float, lot_e2: int) -> int:
    """`x_units` (whole units of the instrument) as a qty_e2 that is a multiple of `lot_e2`,
    with a single half-even rounding of `x_units * 100 / lot_e2`."""
    if lot_e2 <= 0:
        raise ValueError(f"lot_e2 must be positive, got {lot_e2}")
    lots = _round_scaled(x_units, Fraction(100, lot_e2), "to_lot_e2")
    return _check_i64(lots * lot_e2, x_units, "to_lot_e2")  # range-check the PRODUCT, not the count


def cap_text(text: str, limit: int = MAX_TEXT_BYTES) -> str:
    """Truncate to at most `limit` UTF-8 bytes without splitting a code point."""
    return text.encode("utf-8", "replace")[:limit].decode("utf-8", "ignore")


# --- UUIDv7 (RFC 9562 s5.7; Python 3.13's `uuid` has none) -----------------------------------

_last_ms = 0
_seq = 0


def uuid7() -> str:
    """A UUIDv7 string, strictly increasing within this process (RFC 9562 s6.2, method 1: a
    12-bit counter in `rand_a`, advancing the millisecond when it would overflow). Not
    thread-safe; EXO's bus loop is single-threaded asyncio."""
    global _last_ms, _seq
    ms = time.time_ns() // 1_000_000
    if ms > _last_ms:
        _last_ms = ms
        _seq = secrets.randbits(11)  # top bit clear: headroom for >2000 ids in one millisecond
    else:
        _seq += 1
        if _seq > 0xFFF:
            _last_ms += 1
            _seq = secrets.randbits(11)
    value = (_last_ms << 80) | (0x7 << 76) | (_seq << 64) | (0b10 << 62) | secrets.randbits(62)
    return str(uuid.UUID(int=value))


# --- builders ---------------------------------------------------------------------------------


def meta_pb(sent_ns: int, msg_id: str | None = None) -> common_pb2.Meta:
    return common_pb2.Meta(
        msg_id=msg_id if msg_id is not None else uuid7(),
        producer=_PRODUCER,
        sent_ns=sent_ns,
        schema_version=_SCHEMA_VERSION,
    )


def instrument_ref_pb(instrument: Instrument) -> common_pb2.InstrumentRef:
    return common_pb2.InstrumentRef(
        instrument_id=instrument.instrument_id,
        symbol=instrument.symbol,
        instrument_class=f"INSTRUMENT_CLASS_{instrument.instrument_class}",  # enum label
        currency=instrument.currency,
    )


def valuation_meta_pb(fields: ValuationMetaFields) -> common_pb2.ValuationMeta:
    """Field-by-field copy of the reproducibility triple (root CLAUDE.md invariant #7)."""
    return common_pb2.ValuationMeta(
        model_id=fields.model_id,
        params_hash=fields.params_hash,
        seed=fields.seed,
        n_paths=fields.n_paths,
        git_sha=fields.git_sha,
    )


def target_position_pb(
    *,
    book_id: int,
    instrument: Instrument,
    target_qty_e2: int,
    as_of_ns: int,
    band_qty_e2: int,
    valuation: ValuationMetaFields,
    reason: str,
    sent_ns: int,
) -> live_pb2.TargetPosition:
    return live_pb2.TargetPosition(
        meta=meta_pb(sent_ns),
        book_id=book_id,
        instrument=instrument_ref_pb(instrument),
        target_qty_e2=target_qty_e2,
        as_of_ns=as_of_ns,
        band_qty_e2=band_qty_e2,
        valuation=valuation_meta_pb(valuation),
        reason=cap_text(reason),
    )


def valuation_snapshot_pb(
    *,
    book_id: int,
    as_of_ns: int,
    sent_ns: int,
    valuation: ValuationMetaFields,
    lines: Sequence[LineValues],
) -> live_pb2.ValuationSnapshot:
    """Every value is the desk-position value in ADR-014 s9 units; `book_pv_e9` is the sum of the
    ROUNDED line `pv_e9` so the lines add up to the total exactly."""
    if len(lines) > MAX_SNAPSHOT_LINES:
        raise ValueError(f"{len(lines)} lines exceeds the ADR-014 s14 cap {MAX_SNAPSHOT_LINES}")
    pb_lines: list[live_pb2.ValuationSnapshot.ProductLine] = []
    for ln in lines:
        flagged = list(ln.flagged)
        if len(flagged) > MAX_FLAGGED_GREEKS:
            raise ValueError(
                f"{ln.product_id}: {len(flagged)} flagged_greeks exceeds the ADR-014 s14 cap "
                f"{MAX_FLAGGED_GREEKS} (a line has at most 6 by construction)"
            )
        pb_lines.append(
            live_pb2.ValuationSnapshot.ProductLine(
                product_id=ln.product_id,
                product_type=ln.product_type,
                underlying=instrument_ref_pb(ln.instrument),
                pv_e9=to_e9(ln.pv.value),
                pv_std_err_e9=to_e9(ln.pv.std_err),
                delta_qty_e2=to_e2(ln.delta.value),
                vega_e9=to_e9(ln.vega.value),
                gamma_qty_e2=to_e2(ln.gamma.value),
                rho_e9=to_e9(ln.rho.value),
                theta_e9=to_e9(ln.theta.value),
                div_sens_e9=to_e9(ln.div_sens.value),
                delta_std_err_qty_e2=to_e2(ln.delta.std_err),
                vega_std_err_e9=to_e9(ln.vega.std_err),
                gamma_std_err_qty_e2=to_e2(ln.gamma.std_err),
                rho_std_err_e9=to_e9(ln.rho.std_err),
                theta_std_err_e9=to_e9(ln.theta.std_err),
                div_sens_std_err_e9=to_e9(ln.div_sens.std_err),
                rho_foreign_e9=to_e9(ln.rho_foreign.value),
                rho_foreign_std_err_e9=to_e9(ln.rho_foreign.std_err),
                flagged_greeks=flagged,
            )
        )
    total_pv_e9 = sum(ln.pv_e9 for ln in pb_lines)
    return live_pb2.ValuationSnapshot(
        meta=meta_pb(sent_ns),  # always a FRESH msg_id: a reused one would be deduped by D1
        book_id=book_id,
        as_of_ns=as_of_ns,
        valuation=valuation_meta_pb(valuation),
        lines=pb_lines,
        # a sum of in-range ints can leave sint64: check the TOTAL too
        book_pv_e9=_check_i64(total_pv_e9, total_pv_e9, "book_pv_e9"),
    )


def recon_alert_pb(
    *,
    book_id: int,
    instrument: Instrument,
    exo_view_qty_e2: int,
    d1_view_qty_e2: int,
    publishing_halted: bool,
    sent_ns: int,
) -> live_pb2.ReconAlert:
    return live_pb2.ReconAlert(
        meta=meta_pb(sent_ns),
        book_id=book_id,
        instrument=instrument_ref_pb(instrument),
        exo_view_qty_e2=exo_view_qty_e2,
        d1_view_qty_e2=d1_view_qty_e2,
        publishing_halted=publishing_halted,
    )


def health_pb(*, ok: bool, detail: Mapping[str, str], sent_ns: int) -> live_pb2.HealthStatus:
    return live_pb2.HealthStatus(
        meta=meta_pb(sent_ns),
        component="exo",
        ok=ok,
        detail={cap_text(k): cap_text(v) for k, v in detail.items()},
    )


# --- decoders ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Mark:
    msg_id: str
    instrument_id: int
    mid_px_e9: int
    sent_ns: int  # epoch: the ordering / replay-protection clock (ADR-014 s2)
    feed_ts_ns: int  # D1 feed-session clock: lineage only, NOT epoch


def mark_from_pb(pb: live_pb2.MarkPrice) -> Mark:
    if pb.mid_px_e9 <= 0:
        raise ValueError(f"mark mid_px_e9 must be positive, got {pb.mid_px_e9}")
    return Mark(
        msg_id=pb.meta.msg_id,
        instrument_id=pb.instrument.instrument_id,
        mid_px_e9=pb.mid_px_e9,
        sent_ns=pb.meta.sent_ns,
        feed_ts_ns=pb.feed_ts_ns,
    )


@dataclass(frozen=True)
class ExecFill:
    msg_id: str
    cl_ord_id: str
    exec_id: str
    book_id: int
    instrument_id: int
    side: int
    status: int
    last_qty_e2: int
    last_px_e9: int


def exec_fill_from_pb(pb: live_pb2.ExecutionReport) -> ExecFill:
    return ExecFill(
        msg_id=pb.meta.msg_id,
        cl_ord_id=pb.cl_ord_id,
        exec_id=pb.exec_id,
        book_id=pb.book_id,
        instrument_id=pb.instrument.instrument_id,
        side=pb.side,
        status=pb.status,
        last_qty_e2=pb.last_qty_e2,
        last_px_e9=pb.last_px_e9,
    )


@dataclass(frozen=True)
class Cross:
    msg_id: str
    cross_id: str
    instrument_id: int
    buy_book_id: int
    sell_book_id: int
    qty_e2: int
    ref_px_e9: int


def cross_from_pb(pb: live_pb2.InternalCrossNotice) -> Cross:
    return Cross(
        msg_id=pb.meta.msg_id,
        cross_id=pb.cross_id,
        instrument_id=pb.instrument.instrument_id,
        buy_book_id=pb.buy_book_id,
        sell_book_id=pb.sell_book_id,
        qty_e2=pb.qty_e2,
        ref_px_e9=pb.ref_px_e9,
    )


@dataclass(frozen=True)
class PositionLine:
    instrument_id: int
    qty_e2: int
    inflight_qty_e2: int


@dataclass(frozen=True)
class Positions:
    msg_id: str
    book_id: int
    sent_ns: int
    lines: tuple[PositionLine, ...]


def positions_from_pb(pb: live_pb2.PositionSnapshot, *, max_lines: int) -> Positions:
    """`max_lines` is the number of instruments in the universe (ADR-014 s14): a snapshot with
    more lines than the universe has instruments is hostile or corrupt and is rejected."""
    if len(pb.lines) > max_lines:
        raise ValueError(f"PositionSnapshot has {len(pb.lines)} lines, universe has {max_lines}")
    return Positions(
        msg_id=pb.meta.msg_id,
        book_id=pb.book_id,
        sent_ns=pb.meta.sent_ns,
        lines=tuple(
            PositionLine(ln.instrument.instrument_id, ln.qty_e2, ln.inflight_qty_e2)
            for ln in pb.lines
        ),
    )


@dataclass(frozen=True)
class LimitAlert:
    msg_id: str
    instrument_id: int
    greek: int  # live_pb2.RiskLimitAlert.Greek value
    breached: bool
    cleared: bool
    text: str


def limit_alert_from_pb(pb: live_pb2.RiskLimitAlert) -> LimitAlert:
    return LimitAlert(
        msg_id=pb.meta.msg_id,
        instrument_id=pb.underlying.instrument_id,
        greek=pb.greek,
        breached=pb.breached,
        cleared=pb.cleared,
        text=cap_text(pb.text),
    )
