"""Boundary tests for `exo.bus.convert` (exo/CLAUDE.md rule 3): half-even fixed-point conversion on
the exact binary value, sint64 overflow, the lot rounding, string caps, UUIDv7, and the builders
and decoders."""

from __future__ import annotations

import math
import uuid

import pytest
from hypothesis import given
from hypothesis import strategies as st

from exo.bus import convert
from exo.bus.convert import (
    cap_text,
    exec_fill_from_pb,
    health_pb,
    limit_alert_from_pb,
    mark_from_pb,
    positions_from_pb,
    recon_alert_pb,
    target_position_pb,
    to_e2,
    to_e9,
    to_lot_e2,
    uuid7,
    valuation_meta_pb,
    valuation_snapshot_pb,
)
from exo.bus.gen import common_pb2, live_pb2
from exo.greeks.bump import Estimate
from exo.portfolio.aggregate import LineValues
from exo.provenance import ValuationMetaFields
from exo.refdata import Instrument

AAPL = Instrument(1001, "AAPL", "EQUITY", "USD", 100)
META = ValuationMetaFields(
    model_id="heston-qe-pseudo-antithetic-v1", params_hash="h" * 64, seed=2**64 - 1, n_paths=20_000,
    git_sha="abc",
)  # fmt: skip


@pytest.mark.parametrize(
    ("x", "expected"),
    [(0.125, 12), (0.375, 38), (-0.125, -12), (2.5e-1, 25), (0.135, 14), (0.145, 14)],
)
def test_to_e2_half_even_on_exact_binary_ties(x: float, expected: int) -> None:
    """0.125 / 0.375 / -0.125 are exact binary ties: half-even picks the even neighbour. 0.135 and
    0.145 are NOT ties in binary (0.135 is stored a hair above, 0.145 a hair above too), so they
    round by the stored value, not by their decimal spelling."""
    assert to_e2(x) == expected


def test_to_e2_rounds_by_stored_binary_value_not_decimal_spelling() -> None:
    # 2.675 is stored as 2.67499999999999982236431605997495353221893310546875
    assert to_e2(2.675) == 267
    # 2.665 is stored just ABOVE the decimal tie
    assert to_e2(2.665) == 267


def test_to_e9_basic_and_negative_zero() -> None:
    assert to_e9(187.5321) == 187_532_100_000
    assert to_e9(-0.0) == 0
    assert to_e2(-0.0) == 0
    assert str(to_e9(-0.0)) == "0"


@pytest.mark.parametrize("bad", [math.inf, -math.inf, math.nan])
def test_non_finite_raises(bad: float) -> None:
    for fn in (to_e9, to_e2):
        with pytest.raises(ValueError, match="non-finite"):
            fn(bad)
    with pytest.raises(ValueError, match="non-finite"):
        to_lot_e2(bad, 100)


def test_sint64_overflow_raises_at_the_boundary() -> None:
    assert to_e9(9.2e9) == 9_200_000_000_000_000_000  # < 2**63
    with pytest.raises(ValueError, match="overflows sint64"):
        to_e9(9.3e9)  # 9.3e18 > 2**63 - 1
    with pytest.raises(ValueError, match="overflows sint64"):
        to_e9(-9.3e9)
    with pytest.raises(ValueError, match="overflows sint64"):
        to_e2(1e17)  # 1e19 > 2**63 - 1
    assert to_e2(9e16) == 9_000_000_000_000_000_000


def test_to_lot_e2_half_even_on_the_lot() -> None:
    assert to_lot_e2(8044.65, 100) == 804_500
    assert to_lot_e2(8044.5, 100) == 804_400  # exact tie -> even lot count (8044)
    assert to_lot_e2(8045.5, 100) == 804_600  # exact tie -> even lot count (8046)
    assert to_lot_e2(-8044.5, 100) == -804_400
    assert to_lot_e2(0.0, 100) == 0
    assert to_lot_e2(-0.0, 100) == 0


def test_to_lot_e2_is_a_single_rounding_not_two() -> None:
    # 0.49 units = 49 e2; rounding to e2 first and then to a 100-lot would be 49 -> 0 as well, but
    # 0.5 units = 50 e2 is a tie at the lot (0 lots, even), while 0.5000001 is above it.
    assert to_lot_e2(0.5, 100) == 0
    assert to_lot_e2(0.5000001, 100) == 100
    # a multi-unit lot: 250 e2 lot, 1.25 units = 125 e2 = exactly half a lot -> 0
    assert to_lot_e2(1.25, 250) == 0
    assert to_lot_e2(3.75, 250) == 500  # 1.5 lots -> tie -> 2 lots


def test_to_lot_e2_rejects_non_positive_lot() -> None:
    with pytest.raises(ValueError, match="lot_e2"):
        to_lot_e2(1.0, 0)


@given(st.floats(min_value=-1e6, max_value=1e6, allow_nan=False))
def test_to_e2_within_half_a_unit_of_exact(x: float) -> None:
    assert abs(to_e2(x) - x * 100) <= 0.5 + 1e-6 * max(1.0, abs(x))


@given(st.floats(min_value=-1e5, max_value=1e5, allow_nan=False), st.sampled_from([1, 100, 250]))
def test_to_lot_e2_is_a_lot_multiple_within_half_a_lot(x: float, lot: int) -> None:
    n = to_lot_e2(x, lot)
    assert n % lot == 0
    assert abs(n - x * 100) <= lot / 2 + 1e-6 * max(1.0, abs(x * 100))


def test_cap_text_truncates_on_a_code_point_boundary() -> None:
    text = "é" * 150  # 300 bytes
    capped = cap_text(text)
    assert len(capped.encode()) == 256  # 128 whole code points; no split
    assert capped == "é" * 128
    odd = "a" + "é" * 150  # 301 bytes: byte 256 would split a code point
    assert cap_text(odd) == "a" + "é" * 127
    assert cap_text("short") == "short"
    assert cap_text("x" * 300) == "x" * 256


def test_uuid7_version_variant_and_monotonic_order() -> None:
    ids = [uuid7() for _ in range(1000)]
    assert ids == sorted(ids)
    assert len(set(ids)) == 1000
    for s in ids[:50]:
        u = uuid.UUID(s)
        assert u.version == 7
        assert u.variant == uuid.RFC_4122


def test_uuid7_counter_overflow_advances_the_millisecond() -> None:
    convert._last_ms = 2**47
    convert._seq = 0xFFF
    a = uuid7()
    b = uuid7()
    assert a < b
    assert uuid.UUID(a).version == 7
    convert._last_ms = 0
    convert._seq = 0


def _line(pid: str = "P1", *, flagged: tuple[str, ...] = ("delta",)) -> LineValues:
    e = Estimate
    return LineValues(
        product_id=pid, product_type="autocallable", instrument=AAPL,
        pv=e(-1_234_567.891, 100.5), delta=e(8044.65, 580.5), gamma=e(-12.5, 3.0),
        vega=e(22108.0, 1500.0), rho=e(337.0, 20.0), div_sens=e(-5000.0, 900.0),
        rho_foreign=e(0.0, 0.0), theta=e(-1500.0, 400.0), flagged=flagged,
    )  # fmt: skip


def test_valuation_snapshot_pb_units_and_book_pv_is_sum_of_rounded_lines() -> None:
    lines = [_line("P1"), _line("P2")]
    pb = valuation_snapshot_pb(book_id=3, as_of_ns=5, sent_ns=6, valuation=META, lines=lines)
    ln = pb.lines[0]
    assert ln.pv_e9 == to_e9(-1_234_567.891)
    assert ln.delta_qty_e2 == 804_465
    assert ln.gamma_qty_e2 == -1250
    assert ln.delta_std_err_qty_e2 == 58_050
    assert ln.vega_e9 == 22_108_000_000_000
    assert list(ln.flagged_greeks) == ["delta"]
    assert ln.underlying.instrument_id == 1001
    assert pb.book_pv_e9 == pb.lines[0].pv_e9 + pb.lines[1].pv_e9
    assert pb.valuation.seed == 2**64 - 1
    assert pb.meta.producer == "exo"
    assert pb.meta.schema_version == 1
    assert uuid.UUID(pb.meta.msg_id).version == 7
    round_trip = live_pb2.ValuationSnapshot()
    round_trip.ParseFromString(pb.SerializeToString())
    assert round_trip == pb


def test_valuation_snapshot_pb_enforces_adr014_caps() -> None:
    with pytest.raises(ValueError, match="cap"):
        valuation_snapshot_pb(
            book_id=3, as_of_ns=1, sent_ns=1, valuation=META,
            lines=[_line(str(i)) for i in range(1025)],
        )  # fmt: skip
    with pytest.raises(ValueError, match="flagged_greeks"):
        valuation_snapshot_pb(
            book_id=3, as_of_ns=1, sent_ns=1, valuation=META,
            lines=[_line(flagged=tuple("abcdefghi"))],
        )  # fmt: skip


def test_flagged_greeks_cap_is_8_and_book_pv_overflow_raises() -> None:
    ok = valuation_snapshot_pb(
        book_id=3, as_of_ns=1, sent_ns=1, valuation=META,
        lines=[_line(flagged=tuple("abcdefgh"))],
    )  # fmt: skip
    assert len(ok.lines[0].flagged_greeks) == 8
    big = LineValues(**{**_line().__dict__, "pv": Estimate(8e9, 1.0)})  # 8e18 e9 each: fits
    with pytest.raises(ValueError, match=r"book_pv_e9.*16000000000000000000"):
        valuation_snapshot_pb(
            book_id=3, as_of_ns=1, sent_ns=1, valuation=META, lines=[big, big]
        )  # 1.6e19 > 2**63


def test_target_position_pb_caps_reason_and_copies_valuation_meta() -> None:
    pb = target_position_pb(
        book_id=3, instrument=AAPL, target_qty_e2=-804_500, as_of_ns=9, band_qty_e2=50_000,
        valuation=META, reason="é" * 300, sent_ns=10,
    )  # fmt: skip
    assert len(pb.reason.encode()) <= 256
    assert pb.target_qty_e2 == -804_500
    assert pb.valuation == valuation_meta_pb(META)
    assert pb.instrument.instrument_class == common_pb2.INSTRUMENT_CLASS_EQUITY


def test_recon_alert_and_health_builders() -> None:
    ra = recon_alert_pb(
        book_id=3, instrument=AAPL, exo_view_qty_e2=1, d1_view_qty_e2=2, publishing_halted=True,
        sent_ns=1,
    )  # fmt: skip
    assert ra.publishing_halted and ra.d1_view_qty_e2 == 2
    hp = health_pb(ok=False, detail={"k": "é" * 300}, sent_ns=1)
    assert hp.component == "exo" and not hp.ok
    assert len(hp.detail["k"].encode()) <= 256


def test_mark_decoder_reads_feed_ts_and_sent_ns_and_rejects_non_positive() -> None:
    pb = live_pb2.MarkPrice(
        meta=common_pb2.Meta(msg_id="m", sent_ns=77),
        instrument=common_pb2.InstrumentRef(instrument_id=1001),
        mid_px_e9=187_510_000_000,
        feed_ts_ns=12_000_000_000,
    )
    mark = mark_from_pb(pb)
    assert (mark.sent_ns, mark.feed_ts_ns, mark.instrument_id) == (77, 12_000_000_000, 1001)
    pb.mid_px_e9 = 0
    with pytest.raises(ValueError, match="positive"):
        mark_from_pb(pb)


def test_positions_decoder_rejects_more_lines_than_the_universe_has_instruments() -> None:
    pb = live_pb2.PositionSnapshot(
        book_id=1,
        lines=[
            live_pb2.PositionSnapshot.Line(
                instrument=common_pb2.InstrumentRef(instrument_id=1000 + i), qty_e2=i
            )
            for i in range(4)
        ],
    )
    assert len(positions_from_pb(pb, max_lines=4).lines) == 4
    with pytest.raises(ValueError, match="4 lines"):
        positions_from_pb(pb, max_lines=3)


def test_fill_and_limit_alert_decoders() -> None:
    fill = exec_fill_from_pb(
        live_pb2.ExecutionReport(
            meta=common_pb2.Meta(msg_id="x"), exec_id="E1", book_id=3, last_qty_e2=-5,
            instrument=common_pb2.InstrumentRef(instrument_id=1001),
        )
    )  # fmt: skip
    assert (fill.exec_id, fill.book_id, fill.last_qty_e2, fill.instrument_id) == ("E1", 3, -5, 1001)
    alert = limit_alert_from_pb(
        live_pb2.RiskLimitAlert(
            underlying=common_pb2.InstrumentRef(instrument_id=1001), cleared=True, text="é" * 300
        )
    )
    assert alert.cleared and len(alert.text.encode()) <= 256


@pytest.mark.parametrize(
    ("x", "expected"),
    [(1 / 1024, 976_562), (3 / 1024, 2_929_688), (-1 / 1024, -976_562), (-3 / 1024, -2_929_688)],
)
def test_to_e9_half_even_on_exact_binary_ties(x: float, expected: int) -> None:
    """m/1024 * 1e9 = m * 976562.5 exactly (1e9 = 2**9 * 5**9), so odd m is an exact tie."""
    assert to_e9(x) == expected


@pytest.mark.parametrize(
    ("x", "expected"), [(0.5, 0), (1.5, 200), (2.5, 200), (-0.5, 0), (-1.5, -200), (-2.5, -200)]
)
def test_to_lot_e2_ties_go_to_even_lot_count_for_both_signs(x: float, expected: int) -> None:
    assert to_lot_e2(x, 100) == expected  # x units is x lots of 100 e2: exact binary ties


def test_to_e2_and_to_e9_sint64_boundary_both_signs() -> None:
    inside, outside = 92_233_720_368_547_744.0, 92_233_720_368_547_760.0  # adjacent doubles
    assert to_e2(inside) == 9_223_372_036_854_774_400
    assert to_e2(-inside) == -9_223_372_036_854_774_400
    with pytest.raises(ValueError, match="overflows sint64"):
        to_e2(outside)  # x100 = 9_223_372_036_854_776_000 > 2**63 - 1
    with pytest.raises(ValueError, match="overflows sint64"):
        to_e2(-outside)
    with pytest.raises(ValueError, match="overflows sint64"):
        to_e9(1e300)


@pytest.mark.parametrize("lot", [1, 100, 250])
def test_to_lot_e2_result_beyond_sint64_raises_instead_of_returning_a_wide_int(lot: int) -> None:
    """The lot multiplication happens AFTER the overflow check on the lot count, so a count that
    fits can still produce a qty_e2 that does not fit: it must raise, never wrap or widen."""
    with pytest.raises(ValueError, match="overflows sint64"):
        to_lot_e2(9e17 if lot > 1 else 1e17, lot)


def test_non_finite_raises_in_every_builder_input() -> None:
    bad = _line()
    bad = LineValues(**{**bad.__dict__, "pv": Estimate(math.nan, 1.0)})
    with pytest.raises(ValueError, match="non-finite"):
        valuation_snapshot_pb(book_id=1, as_of_ns=1, sent_ns=1, valuation=META, lines=[bad])
    huge = LineValues(**{**_line().__dict__, "vega": Estimate(1e300, 1.0)})
    with pytest.raises(ValueError, match="overflows sint64"):
        valuation_snapshot_pb(book_id=1, as_of_ns=1, sent_ns=1, valuation=META, lines=[huge])


def test_adr014_s14_caps_accept_exactly_the_limit() -> None:
    pb = valuation_snapshot_pb(
        book_id=3, as_of_ns=1, sent_ns=1, valuation=META,
        lines=[_line(str(i), flagged=tuple("abcdefgh")) for i in range(1024)],
    )  # fmt: skip
    assert len(pb.lines) == 1024
    assert len(pb.lines[0].flagged_greeks) == 8


def test_cap_text_keeps_exactly_256_bytes_and_caps_health_keys_and_reason_boundary() -> None:
    assert cap_text("x" * 256) == "x" * 256
    assert cap_text("x" * 255 + "é") == "x" * 255  # the 2-byte char would straddle byte 256
    hp = health_pb(ok=True, detail={"k" * 300: "v" * 300}, sent_ns=1)
    ((k, v),) = hp.detail.items()
    assert len(k.encode()) == 256 and len(v.encode()) == 256
    pb = target_position_pb(
        book_id=3, instrument=AAPL, target_qty_e2=0, as_of_ns=1, band_qty_e2=0, valuation=META,
        reason="x" * 255 + "é", sent_ns=1,
    )  # fmt: skip
    assert pb.reason == "x" * 255


def test_valuation_snapshot_pb_has_no_caller_supplied_msg_id_and_never_repeats_one() -> None:
    import inspect

    assert "msg_id" not in inspect.signature(valuation_snapshot_pb).parameters
    kw = {"book_id": 3, "as_of_ns": 1, "sent_ns": 1, "valuation": META, "lines": [_line()]}
    a, b = valuation_snapshot_pb(**kw), valuation_snapshot_pb(**kw)  # type: ignore[arg-type]
    assert a.meta.msg_id != b.meta.msg_id
