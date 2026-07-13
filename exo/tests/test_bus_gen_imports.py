"""Proves the committed protobuf codegen in exo/src/exo/bus/gen is importable and valid."""

from exo.bus.gen import common_pb2, live_pb2


def test_common_message_round_trips() -> None:
    meta = common_pb2.Meta(msg_id="abc", producer="exo", sent_ns=123, schema_version=1)
    wire = meta.SerializeToString()
    parsed = common_pb2.Meta()
    parsed.ParseFromString(wire)
    assert parsed == meta


def test_live_message_references_common() -> None:
    target = live_pb2.TargetPosition(book_id=7, target_qty_e2=100)
    assert target.book_id == 7
    assert target.instrument.instrument_id == 0
