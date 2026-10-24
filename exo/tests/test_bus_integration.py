"""The ONE `just up` integration test of the EXO loop (P2.M4 slice 4).

Needs NATS on 127.0.0.1:4222 (`just up`). Marked `infra` and auto-skipped when the broker is
unreachable, so CI (no Docker) collects it and skips it, like the Rust `#[ignore]` infra tests.
It runs `service.main` for real -- spawned process pool, real `nats-py` -- against a one-warrant
book (4,000 paths, 0.25 y, about a second), acting as a fake Delta One.
"""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import nats
import pytest
from _book_fixtures import EXO_TOML
from google.protobuf.message import Message
from nats.aio.msg import Msg

from exo.bus.convert import uuid7
from exo.bus.gen import live_pb2
from exo.bus.service import main
from exo.config import ExoConfig, ReconConfig, load_config

NATS_URL = "nats://127.0.0.1:4222"
BOOK, AAPL = 3, 1001
_BOOK_TOML = """
[[book]]
code = "EXO-SP"
seed = 7
n_paths = 4000

[[book.product]]
product_id = "EXO-SP-W-IT"
type = "warrant_call"
desk_quantity = -100.0
underlying = "AAPL"
strike = 187.5
expiry = 0.25
ratio = 1.0
exercise = "european"
"""


def _broker_up() -> bool:
    try:
        socket.create_connection(("127.0.0.1", 4222), timeout=0.5).close()
    except OSError:
        return False
    return True


pytestmark = [
    pytest.mark.infra,
    pytest.mark.skipif(not _broker_up(), reason="NATS not reachable on 127.0.0.1:4222 (just up)"),
]


def _config(tmp_path: Path) -> ExoConfig:
    (tmp_path / "book.toml").write_text(_BOOK_TOML)
    cfg = load_config(EXO_TOML)
    return cfg.model_copy(
        update={
            "bus": cfg.bus.model_copy(update={"nats_url": NATS_URL}),
            "valuation": cfg.valuation.model_copy(
                update={"book": tmp_path / "book.toml", "manifest_dir": tmp_path / "manifests"}
            ),
            "recon": ReconConfig(tolerance_qty_e2=0, grace_ms=300, max_snapshot_gap_ms=60_000),
        }
    )


async def _first(queue: asyncio.Queue[Any], mine: Callable[[Any], bool], timeout: float) -> Any:  # noqa: ANN401
    """First message satisfying `mine`. A shared broker may carry another EXO / D1 (a running
    storyline) publishing the same subjects; this test only accepts ITS OWN book's output."""
    async with asyncio.timeout(timeout):
        while True:
            msg = await queue.get()
            if mine(msg):
                return msg


def _meta(msg: live_pb2.PositionSnapshot | live_pb2.MarkPrice) -> None:
    msg.meta.msg_id = uuid7()
    msg.meta.producer = "it-fake-d1"
    msg.meta.sent_ns = time.time_ns()
    msg.meta.schema_version = 1


async def test_marks_in_targets_and_valuations_out_then_recon_halt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXO_GIT_SHA", "a" * 40)  # a clean, deterministic provenance on any tree
    cfg = _config(tmp_path)
    nc = await nats.connect(NATS_URL)
    targets: asyncio.Queue[Any] = asyncio.Queue()
    valuations: asyncio.Queue[Any] = asyncio.Queue()
    recon: asyncio.Queue[Any] = asyncio.Queue()
    health: asyncio.Queue[Any] = asyncio.Queue()

    def sink(queue: asyncio.Queue[Any], cls: type[Message]) -> Callable[[Msg], Awaitable[None]]:
        async def cb(msg: Msg) -> None:
            queue.put_nowait(cls.FromString(msg.data))

        return cb

    await nc.subscribe(f"exo.targets.{BOOK}.{AAPL}", cb=sink(targets, live_pb2.TargetPosition))
    await nc.subscribe(f"exo.valuations.{BOOK}", cb=sink(valuations, live_pb2.ValuationSnapshot))
    await nc.subscribe("exo.alerts.recon", cb=sink(recon, live_pb2.ReconAlert))
    await nc.subscribe("exo.health", cb=sink(health, live_pb2.HealthStatus))
    stop = asyncio.Event()
    service = asyncio.create_task(main(cfg, stop))
    try:
        # EXO subscribes before its first heartbeat, so a heartbeat means we may publish.
        await _first(health, lambda h: h.detail.get("git_sha") == "a" * 40, 180)

        pos = live_pb2.PositionSnapshot(book_id=BOOK)  # empty: flat book
        _meta(pos)
        await nc.publish(f"d1.positions.{BOOK}", pos.SerializeToString())
        mark = live_pb2.MarkPrice(mid_px_e9=187_500_000_000)
        mark.instrument.instrument_id = AAPL
        _meta(mark)
        await nc.publish(f"d1.marks.{AAPL}", mark.SerializeToString())

        own = lambda m: m.valuation.seed == 7 and m.valuation.n_paths == 4000  # noqa: E731
        target = await _first(targets, own, 120)
        snapshot = await _first(valuations, own, 60)
        assert target.book_id == BOOK and target.instrument.instrument_id == AAPL
        assert (
            target.target_qty_e2 > 0 and target.target_qty_e2 % 100 == 0
        )  # short calls: buy delta
        assert target.valuation.git_sha == "a" * 40 and target.valuation.n_paths == 4000
        assert snapshot.valuation == target.valuation and snapshot.valuation.model_id
        assert snapshot.valuation.params_hash and snapshot.valuation.seed == 7
        assert len(snapshot.lines) == 1 and snapshot.meta.producer == "exo"
        assert snapshot.meta.schema_version == 2  # ValuationSnapshot grew (ADR-014 s4)
        assert list((tmp_path / "manifests").glob("*.toml"))

        # A divergent snapshot, twice, more than grace_ms (300) apart: the book halts.
        for _ in range(2):
            div = live_pb2.PositionSnapshot(book_id=BOOK)
            div.lines.add(qty_e2=12_300).instrument.instrument_id = AAPL
            _meta(div)
            await nc.publish(f"d1.positions.{BOOK}", div.SerializeToString())
            await asyncio.sleep(0.5)
        alert = await _first(
            recon, lambda a: a.publishing_halted and a.d1_view_qty_e2 == 12_300, 10
        )
        assert alert.publishing_halted and alert.book_id == BOOK
        assert (alert.exo_view_qty_e2, alert.d1_view_qty_e2) == (0, 12_300)
    finally:
        stop.set()
        await asyncio.wait_for(service, 60)
        await nc.close()
