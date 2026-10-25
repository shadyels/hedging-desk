"""`bus/service.py`: the asyncio shell, driven with a fake bus and a fake (or real) runner."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from _book_fixtures import EXO_TOML
from _state_helpers import AAPL, BOOK, CFG, SHA, UNI, FakeClock, book, fake_result, mark, snapshot
from google.protobuf.message import Message

from exo.bus.gen import live_pb2
from exo.bus.service import ExoService, FatalError, Handler, PoolRunner, _token, _warmup_job
from exo.config import ExoConfig, ProvenanceConfig, ReconConfig
from exo.portfolio.revalue import BookJob, BookResult
from exo.portfolio.state import ExoState
from exo.provenance import ProvenanceError

TARGET = f"exo.targets.{BOOK}.{AAPL}"
VALUATION = f"exo.valuations.{BOOK}"


class FakeBus:
    def __init__(self) -> None:
        self.connected = True
        self.published: list[tuple[str, bytes]] = []
        self.subscriptions: dict[str, Handler] = {}

    async def publish(self, subject: str, payload: bytes) -> None:
        self.published.append((subject, payload))

    async def subscribe(self, subject: str, handler: Handler) -> None:
        self.subscriptions[subject] = handler

    def on(self, subject: str) -> list[Message]:
        kinds = {
            "exo.targets": live_pb2.TargetPosition,
            "exo.valuations": live_pb2.ValuationSnapshot,
            "exo.alerts": live_pb2.ReconAlert,
        }
        cls: Any = next(c for p, c in kinds.items() if subject.startswith(p))
        return [cls.FromString(b) for s, b in self.published if s == subject]


class FakeRunner:
    def __init__(self, targets: dict[int, int] | None = None) -> None:
        self.targets = targets or {AAPL: 1_000_000}
        self.calls = 0
        self.fail: Exception | None = None
        self.gate: asyncio.Event | None = None

    async def __call__(self, job: BookJob) -> BookResult:
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.fail is not None:
            raise self.fail
        return fake_result(job.book, self.targets, created_ns=job.created_ns, sha=job.git_sha)


def make_service(
    tmp_path: Path,
    *,
    runner: FakeRunner | None = None,
    sha: str = SHA,
    cfg: ExoConfig = CFG,
    sha_fn: Callable[[], str] | None = None,
) -> tuple[ExoService, FakeBus, FakeRunner, FakeClock]:
    cfg = cfg.model_copy(
        update={"valuation": cfg.valuation.model_copy(update={"manifest_dir": tmp_path / "m"})}
    )
    bus, runner, clock = FakeBus(), runner or FakeRunner(), FakeClock()
    state = ExoState(cfg, UNI, (book(),), clock, sha)
    svc = ExoService(cfg, bus, runner, state=state, clock=clock, sha_fn=sha_fn or (lambda: sha))
    return svc, bus, runner, clock


async def feed(svc: ExoService, *, snap: bool = True, mid_e9: int = 187_500_000_000) -> None:
    if snap:
        pos = snapshot({}, sent_ns=1)
        pb = live_pb2.PositionSnapshot(book_id=BOOK)
        pb.meta.msg_id, pb.meta.sent_ns = pos.msg_id, pos.sent_ns
        await svc.handle(f"d1.positions.{BOOK}", pb.SerializeToString())
    m = mark(mid_e9=mid_e9, sent_ns=2)
    mpb = live_pb2.MarkPrice(mid_px_e9=m.mid_px_e9)
    mpb.meta.msg_id, mpb.meta.sent_ns = m.msg_id, m.sent_ns
    mpb.instrument.instrument_id = AAPL
    await svc.handle(f"d1.marks.{AAPL}", mpb.SerializeToString())


def manifests(tmp_path: Path) -> list[Path]:
    d = tmp_path / "m"
    return sorted(d.glob("*.toml")) if d.exists() else []


def test_token_accepts_only_canonical_positive_ids() -> None:
    assert _token("3") == 3 and _token("1001") == 1001
    for bad in ("0", "03", "+3", "-3", "3.0", "", "x", "٣", "AAPL"):
        assert _token(bad) is None, bad


async def test_subscriptions_are_exactly_marks_positions_exec_crosses_limits(
    tmp_path: Path,
) -> None:
    svc, bus, _, _ = make_service(tmp_path)
    await svc.subscribe_all()
    assert set(bus.subscriptions) == {
        f"d1.marks.{AAPL}",
        f"d1.positions.{BOOK}",
        f"d1.exec.{BOOK}.>",
        "d1.crosses",
        "d1.alerts.limits",
    }


async def test_end_to_end_marks_to_target_with_one_manifest_per_real_revaluation(
    tmp_path: Path,
) -> None:
    svc, bus, runner, clock = make_service(tmp_path)
    await feed(svc)
    await svc.revalue_pending()
    (target,) = bus.on(TARGET)
    (snap,) = bus.on(VALUATION)
    assert target.target_qty_e2 == 1_000_000 and target.valuation.git_sha == SHA  # type: ignore[attr-defined]
    assert snap.valuation == target.valuation  # type: ignore[attr-defined]
    assert snap.valuation.model_id and snap.valuation.params_hash  # type: ignore[attr-defined]
    assert runner.calls == 1 and len(manifests(tmp_path)) == 1
    # I1: the interval republishes, no job, no manifest, fresh msg_id, same as_of
    clock.advance_ms(5000)
    await svc.interval_tick()
    first, second = bus.on(VALUATION)
    assert second.as_of_ns == first.as_of_ns and second.meta.msg_id != first.meta.msg_id  # type: ignore[attr-defined]
    assert runner.calls == 1 and len(manifests(tmp_path)) == 1


async def test_r13_interval_republish_fires_while_a_revaluation_is_in_flight(
    tmp_path: Path,
) -> None:
    svc, bus, runner, clock = make_service(tmp_path)
    await feed(svc)
    await svc.revalue_pending()
    clock.advance_ms(1100)  # past the per-book pacing gap
    runner.gate = asyncio.Event()
    await feed(svc, snap=False, mid_e9=190 * 10**9)
    job = asyncio.create_task(svc.revalue_pending())
    for _ in range(50):
        await asyncio.sleep(0)
        if svc.state.in_flight:
            break
    assert svc.state.in_flight and not job.done()
    before = len(bus.on(VALUATION))
    await svc.interval_tick()
    await svc.interval_tick()
    assert len(bus.on(VALUATION)) == before + 2 and not job.done()
    runner.gate.set()
    await job
    assert not svc.state.in_flight and len(bus.on(VALUATION)) == before + 3


async def test_a_revaluation_exception_skips_that_cycle_and_the_loop_survives(
    tmp_path: Path,
) -> None:
    svc, bus, runner, clock = make_service(tmp_path)
    runner.fail = ValueError("bad mark")
    await feed(svc)
    await svc.revalue_pending()
    assert bus.on(VALUATION) == [] and bus.on(TARGET) == [] and manifests(tmp_path) == []
    detail = svc.state.health(connected=True)[1]
    assert "bad mark" in detail[f"book.{BOOK}.last_error"]
    runner.fail = None
    clock.advance_ms(1100)
    await feed(svc, snap=False, mid_e9=191 * 10**9)  # the next mark retries (after the gap)
    await svc.revalue_pending()
    assert len(bus.on(TARGET)) == 1
    assert f"book.{BOOK}.last_error" not in svc.state.health(connected=True)[1]


async def test_an_unwritable_manifest_dir_skips_the_publish(tmp_path: Path) -> None:
    svc, bus, _, _ = make_service(tmp_path)
    (tmp_path / "m").write_text("a file where the directory should be")
    await feed(svc)
    await svc.revalue_pending()
    assert bus.on(VALUATION) == [] and bus.on(TARGET) == []


async def test_d1_dirty_tree_publishes_neither_targets_nor_snapshots(tmp_path: Path) -> None:
    svc, bus, runner, _ = make_service(tmp_path, sha=SHA + "-dirty")
    await feed(svc)
    await svc.revalue_pending()
    await svc.interval_tick()
    await svc.heartbeat_tick()
    assert runner.calls == 0 and bus.on(VALUATION) == [] and bus.on(TARGET) == []
    health = live_pb2.HealthStatus.FromString(bus.published[-1][1])
    assert health.component == "exo" and not health.ok
    assert health.detail["publish"].startswith("refused: dirty tree")


async def test_d1_allow_dirty_publishes_with_the_dirty_suffix_on_the_wire(tmp_path: Path) -> None:
    cfg = CFG.model_copy(update={"provenance": ProvenanceConfig(allow_dirty=True)})
    svc, bus, _, _ = make_service(tmp_path, sha=SHA + "-dirty", cfg=cfg)
    await feed(svc)
    await svc.revalue_pending()
    assert bus.on(TARGET)[0].valuation.git_sha == SHA + "-dirty"  # type: ignore[attr-defined]
    assert bus.on(VALUATION)[0].valuation.git_sha == SHA + "-dirty"  # type: ignore[attr-defined]


async def test_unavailable_provenance_refuses_to_publish(tmp_path: Path) -> None:
    def broken() -> str:
        raise ProvenanceError("no git")

    svc, bus, runner, _ = make_service(tmp_path, sha_fn=broken)
    await feed(svc)
    await svc.revalue_pending()
    assert runner.calls == 0 and bus.published == []
    assert svc.state.health(connected=True)[1]["publish"].startswith("refused")


async def test_garbage_and_hostile_payloads_are_counted_and_never_fatal(tmp_path: Path) -> None:
    svc, bus, _, _ = make_service(tmp_path)
    await svc.handle(f"d1.marks.{AAPL}", b"\xff\xff\xff not protobuf")
    await svc.handle("d1.crosses", b"\x0a\xff")
    await svc.handle("d1.mystery", b"")
    await svc.handle(f"d1.marks.{AAPL}", live_pb2.MarkPrice().SerializeToString())  # mid 0
    over = live_pb2.PositionSnapshot(book_id=BOOK)
    over.meta.msg_id = "m"
    for i in range(len(UNI.instruments) + 1):
        over.lines.add().instrument.instrument_id = 1001 + i
    await svc.handle(f"d1.positions.{BOOK}", over.SerializeToString())
    assert svc.state.rejects["decode"] == 4 and svc.state.rejects["subject"] == 1
    assert svc.state.ledger_of(BOOK) is None and bus.published == []


async def test_fills_and_crosses_flow_through_the_decoders_into_the_ledger(tmp_path: Path) -> None:
    svc, _, _, _ = make_service(tmp_path)
    await feed(svc)
    rep = live_pb2.ExecutionReport(
        cl_ord_id="c", exec_id="e1", book_id=BOOK, side=1, status=live_pb2.ORD_STATUS_FILLED,
        last_qty_e2=500,
    )  # fmt: skip
    rep.meta.msg_id = "m1"
    rep.instrument.instrument_id = AAPL
    await svc.handle(f"d1.exec.{BOOK}.{AAPL}", rep.SerializeToString())
    cr = live_pb2.InternalCrossNotice(cross_id="x1", buy_book_id=1, sell_book_id=BOOK, qty_e2=200)
    cr.meta.msg_id = "m2"
    cr.instrument.instrument_id = AAPL
    await svc.handle("d1.crosses", cr.SerializeToString())
    assert svc.state.ledger_of(BOOK) == {AAPL: 300}


async def test_heartbeat_reports_the_adr_keys(tmp_path: Path) -> None:
    svc, bus, _, _ = make_service(tmp_path)
    await feed(svc)
    await svc.revalue_pending()
    await svc.heartbeat_tick()
    h = live_pb2.HealthStatus.FromString(bus.published[-1][1])
    assert h.ok and h.component == "exo"
    assert h.detail["git_sha"] == SHA and h.detail["publish"] == "enabled"
    assert (
        h.detail[f"book.{BOOK}.state"] == "publishing"
        and h.detail["valuation_in_flight"] == "false"
    )
    assert int(h.detail[f"book.{BOOK}.last_valuation_ns"]) > 0
    bus.connected = False
    await svc.heartbeat_tick()
    assert not live_pb2.HealthStatus.FromString(bus.published[-1][1]).ok


async def test_recon_halt_through_the_bus_alerts_once_stops_targets_and_keeps_valuations(
    tmp_path: Path,
) -> None:
    cfg = CFG.model_copy(
        update={"recon": ReconConfig(tolerance_qty_e2=0, grace_ms=0, max_snapshot_gap_ms=60_000)}
    )
    svc, bus, runner, clock = make_service(tmp_path, cfg=cfg)
    await feed(svc)
    await svc.revalue_pending()
    assert len(bus.on(TARGET)) == 1 and len(bus.on(VALUATION)) == 1

    async def snap_with(qty: int, sent_ns: int) -> None:
        pos = live_pb2.PositionSnapshot(book_id=BOOK)
        pos.meta.msg_id, pos.meta.sent_ns = f"snap-{sent_ns}", sent_ns
        if qty:
            line = pos.lines.add()
            line.instrument.instrument_id, line.qty_e2 = AAPL, qty
        await svc.handle(f"d1.positions.{BOOK}", pos.SerializeToString())

    await snap_with(100, 3)
    await snap_with(100, 4)
    alerts = bus.on("exo.alerts.recon")
    assert len(alerts) == 1 and alerts[0].publishing_halted  # type: ignore[attr-defined]
    await svc.heartbeat_tick()
    h = live_pb2.HealthStatus.FromString(bus.published[-1][1])
    assert not h.ok and h.detail[f"book.{BOOK}.state"] == "halted:recon"
    runner.targets = {AAPL: 9_000_000}  # far outside the band
    clock.advance_ms(1100)
    await feed(svc, snap=False, mid_e9=190_000_000_000)
    await svc.revalue_pending()
    assert len(bus.on(TARGET)) == 1 and len(bus.on(VALUATION)) == 2  # snapshots continue
    clock.advance_ms(1000)
    await snap_with(0, 5)  # a later agreeing snapshot clears
    assert [a.publishing_halted for a in bus.on("exo.alerts.recon")] == [True, False]  # type: ignore[attr-defined]
    assert bus.on(TARGET)[-1].target_qty_e2 == 9_000_000  # type: ignore[attr-defined]


async def test_run_publishes_exo_health_on_a_one_second_heartbeat(tmp_path: Path) -> None:
    svc, bus, _, _ = make_service(tmp_path)
    stop = asyncio.Event()
    task = asyncio.create_task(svc.run(stop))
    for _ in range(300):  # initial beat + the 1.0 s timer
        if sum(s == "exo.health" for s, _ in bus.published) >= 2:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 5)
    assert sum(s == "exo.health" for s, _ in bus.published) >= 2


async def test_run_wires_the_worker_and_timers_and_stops_cleanly(tmp_path: Path) -> None:
    cfg = CFG.model_copy(
        update={"valuation": CFG.valuation.model_copy(update={"revaluation_interval_s": 0.02})}
    )
    svc, bus, _, _ = make_service(tmp_path, cfg=cfg)
    stop = asyncio.Event()
    runner_task = asyncio.create_task(svc.run(stop))
    await asyncio.sleep(0.01)
    await feed(svc)  # handle() wakes the valuation worker
    for _ in range(200):
        if len(bus.on(VALUATION)) >= 2:  # one real + at least one interval republish
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(runner_task, 5)
    assert len(bus.on(TARGET)) == 1 and len(bus.on(VALUATION)) >= 2
    assert any(s == "exo.health" for s, _ in bus.published)


async def test_the_real_process_pool_runs_a_job_end_to_end() -> None:
    """Pickling + spawn + the worker's own imports, which nothing else exercises."""
    runner = PoolRunner()
    try:
        job = _warmup_job(CFG, book(), SHA, 1_792_000_000_000_000_000)
        result = await asyncio.wait_for(runner(job), 120)
        assert set(result.targets) == {AAPL} and result.manifest.git_sha == SHA
    finally:
        runner.shutdown()


@pytest.mark.parametrize("field,value", [("side", 7), ("status", 99)])
async def test_unknown_enum_values_are_rejected_from_the_wire_bytes(
    tmp_path: Path, field: str, value: int
) -> None:
    """Real protobuf decode (proto3 enums are open: 7 / 99 survive parsing) into the handler."""
    svc, _, _, _ = make_service(tmp_path)
    await feed(svc)
    rep = live_pb2.ExecutionReport(
        cl_ord_id="c", exec_id="e1", book_id=BOOK, side=1, status=live_pb2.ORD_STATUS_FILLED,
        last_qty_e2=500,
    )  # fmt: skip
    setattr(rep, field, value)
    rep.meta.msg_id = "m-enum"
    rep.instrument.instrument_id = AAPL
    wire = rep.SerializeToString()
    assert getattr(live_pb2.ExecutionReport.FromString(wire), field) == value
    await svc.handle(f"d1.exec.{BOOK}.{AAPL}", wire)
    assert svc.state.ledger_of(BOOK) == {} and svc.state.rejects["exec"] == 1


async def test_failure_stops_the_interval_republish_and_a_later_success_resumes_it(
    tmp_path: Path,
) -> None:
    svc, bus, runner, clock = make_service(tmp_path)
    await feed(svc)
    await svc.revalue_pending()
    runner.fail = RuntimeError("boom")
    clock.advance_ms(1100)
    await feed(svc, snap=False, mid_e9=190 * 10**9)
    await svc.revalue_pending()
    n = len(bus.on(VALUATION))
    await svc.interval_tick()
    assert len(bus.on(VALUATION)) == n  # nothing republished while the book is failed
    await svc.heartbeat_tick()
    h = live_pb2.HealthStatus.FromString(bus.published[-1][1])
    assert not h.ok and "boom" in h.detail[f"book.{BOOK}.last_error"]
    runner.fail = None
    clock.advance_ms(10_000)  # the retry timer fires (no new mark needed)
    await svc.revalue_pending()
    await svc.interval_tick()
    assert len(bus.on(VALUATION)) == n + 2  # the new valuation and the resumed republish


async def test_a_hung_job_is_fatal_one_ok_false_health_and_a_nonzero_exit(tmp_path: Path) -> None:
    cfg = CFG.model_copy(
        update={"valuation": CFG.valuation.model_copy(update={"job_timeout_s": 0.05})}
    )
    svc, bus, runner, _ = make_service(tmp_path, cfg=cfg)
    runner.gate = asyncio.Event()  # never set: the job hangs
    stop = asyncio.Event()
    task = asyncio.create_task(svc.run(stop))
    await asyncio.sleep(0.01)
    await feed(svc)
    with pytest.raises(FatalError, match="valuation hung"):
        await asyncio.wait_for(task, 10)
    beats = [live_pb2.HealthStatus.FromString(b) for s, b in bus.published if s == "exo.health"]
    last = beats[-1]
    assert not last.ok and last.detail["fatal"] == "valuation hung"
    assert "valuation hung" in last.detail[f"book.{BOOK}.last_error"]
    assert sum(not b.ok and "fatal" in b.detail for b in beats) == 1  # exactly ONE fatal beat
    await svc.heartbeat_tick()  # no further beats once fatal
    assert len([s for s, _ in bus.published if s == "exo.health"]) == len(beats)
    assert bus.on(TARGET) == [] and bus.on(VALUATION) == []


def test_the_cli_exits_nonzero_on_a_fatal_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import exo.__main__ as entry

    async def dying(cfg: object) -> None:
        raise FatalError("valuation hung")

    monkeypatch.setattr(entry, "main", dying)
    with pytest.raises(SystemExit) as exc:
        entry.cli(["--config", str(EXO_TOML)])
    assert exc.value.code not in (0, None)


async def test_a_runners_own_timeout_error_is_an_ordinary_failure_not_fatal(
    tmp_path: Path,
) -> None:
    svc, bus, runner, _ = make_service(tmp_path)
    runner.fail = TimeoutError("inner")
    await feed(svc)
    await svc.revalue_pending()  # does not raise FatalError
    assert "TimeoutError" in svc.state.health(connected=True)[1][f"book.{BOOK}.last_error"]


async def test_a_failed_target_publish_is_rolled_back_and_retried_by_the_next_valuation(
    tmp_path: Path,
) -> None:
    svc, bus, runner, clock = make_service(tmp_path)
    real_publish = bus.publish
    failing = {"on": True}

    async def flaky(subject: str, payload: bytes) -> None:
        if failing["on"] and subject.startswith("exo.targets"):
            raise ConnectionError("down")
        await real_publish(subject, payload)

    bus.publish = flaky  # type: ignore[method-assign]
    await feed(svc)
    await svc.revalue_pending()
    assert bus.on(TARGET) == [] and len(bus.on(VALUATION)) == 1
    failing["on"] = False
    runner.targets = {AAPL: 1_010_000}  # in band of the phantom 1_000_000
    clock.advance_ms(1100)
    await feed(svc, snap=False, mid_e9=190 * 10**9)
    await svc.revalue_pending()
    assert [t.target_qty_e2 for t in bus.on(TARGET)] == [1_010_000]  # type: ignore[attr-defined]


async def test_a_dirty_tree_does_not_wake_the_worker_or_run_git_per_message(
    tmp_path: Path,
) -> None:
    calls = {"n": 0}

    def counting() -> str:
        calls["n"] += 1
        return SHA + "-dirty"

    svc, _, _, _ = make_service(tmp_path, sha=SHA + "-dirty", sha_fn=counting)
    stop = asyncio.Event()
    task = asyncio.create_task(svc.run(stop))
    await asyncio.sleep(0.01)
    for i in range(20):
        await feed(svc, snap=(i == 0), mid_e9=(100 + i) * 10**9)
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 5)
    assert calls["n"] == 0  # no message-driven git; only the (5 s) interval tick would call it


async def test_handler_bugs_are_counted_and_rate_limited_not_per_message_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    svc, _, _, _ = make_service(tmp_path)

    def boom(*_: object) -> None:
        raise KeyError("bug")

    svc.state.on_mark = boom  # type: ignore[method-assign]
    with caplog.at_level("WARNING"):
        for i in range(50):
            await feed(svc, snap=False, mid_e9=(100 + i) * 10**9)
    assert svc.state.rejects["handler"] == 50
    assert sum("rejected handler" in r.message for r in caplog.records) == 10


async def test_a_drain_failure_during_an_outage_still_closes_and_shuts_the_pool_down() -> None:
    from exo.bus import service as svc_mod

    events: list[str] = []

    class NC:
        is_closed = False

        async def drain(self) -> None:
            raise RuntimeError("reconnecting")

        async def close(self) -> None:
            events.append("close")

    class Runner:
        async def __call__(self, job: BookJob) -> BookResult:
            return fake_result(job.book, {AAPL: 1}, created_ns=job.created_ns, sha=job.git_sha)

        def shutdown(self) -> None:
            events.append("shutdown")

        def kill(self) -> None:
            events.append("kill")

    stop = asyncio.Event()
    stop.set()
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(svc_mod, "PoolRunner", Runner)

        async def fake_connect(*a: object, **k: object) -> NC:
            return NC()

        mp.setattr(svc_mod.nats, "connect", fake_connect)

        async def no_run(self: object, stop: asyncio.Event) -> None:
            return None

        mp.setattr(svc_mod.ExoService, "run", no_run)
        mp.setenv("EXO_GIT_SHA", "a" * 40)
        await svc_mod.main(CFG, stop)
    finally:
        mp.undo()
    assert events == ["close", "shutdown"]


async def test_pool_runner_kill_terminates_the_worker() -> None:
    runner = PoolRunner()
    await asyncio.wait_for(runner(_warmup_job(CFG, book(), SHA, 1)), 120)
    procs = list(runner._pool._processes.values())
    assert procs and all(p.is_alive() for p in procs)
    runner.kill()
    for p in procs:
        p.join(10)
    assert not any(p.is_alive() for p in procs)


class _FakeProc:
    def __init__(self, *, dies_on_term: bool, term_raises: bool = False) -> None:
        self.dies_on_term, self.term_raises = dies_on_term, term_raises
        self.alive, self.calls = True, []

    def terminate(self) -> None:
        self.calls.append("terminate")
        if self.term_raises:
            raise OSError("gone")
        self.alive = not self.dies_on_term

    def join(self, timeout: float) -> None:
        self.calls.append(f"join({timeout})")

    def is_alive(self) -> bool:
        return self.alive

    def kill(self) -> None:
        self.calls.append("kill")
        self.alive = False


class _FakePool:
    def __init__(self, procs: dict[int, _FakeProc] | None) -> None:
        self._processes = procs
        self.shut = False

    def shutdown(self, **_: object) -> None:
        self.shut = True


def test_kill_escalates_to_sigkill_only_for_a_worker_that_survives_sigterm() -> None:
    polite, stubborn = _FakeProc(dies_on_term=True), _FakeProc(dies_on_term=False)
    runner = PoolRunner()
    runner._pool.shutdown(wait=False)
    pool = _FakePool({1: polite, 2: stubborn})
    runner._pool = pool  # type: ignore[assignment]
    runner.kill()
    assert polite.calls == ["terminate", "join(2)"]
    assert stubborn.calls == ["terminate", "join(2)", "kill"] and pool.shut


def test_kill_survives_a_vanished_process_table_and_a_failing_terminate() -> None:
    runner = PoolRunner()
    runner._pool.shutdown(wait=False)
    runner._pool = _FakePool(None)  # type: ignore[assignment]  # 3.13 sets it to None on shutdown
    runner.kill()  # no AttributeError
    bad = _FakeProc(dies_on_term=False, term_raises=True)
    runner._pool = _FakePool({1: bad, 2: _FakeProc(dies_on_term=True)})  # type: ignore[assignment]
    runner.kill()
    assert bad.calls[0] == "terminate"


async def test_messages_inside_the_pacing_gap_run_no_git_and_are_picked_up_after_it(
    tmp_path: Path,
) -> None:
    calls = {"n": 0}

    def counting() -> str:
        calls["n"] += 1
        return SHA

    svc, bus, runner, clock = make_service(tmp_path, sha_fn=counting)
    stop = asyncio.Event()
    task = asyncio.create_task(svc.run(stop))
    await asyncio.sleep(0.01)
    await feed(svc)
    for _ in range(100):
        if runner.calls == 1:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.02)
    before = calls["n"]
    assert runner.calls == 1 and before >= 1
    for i in range(10):  # inside the 1 s gap (the fake clock has not moved)
        await feed(svc, snap=False, mid_e9=(200 + i) * 10**9)
    await asyncio.sleep(0.05)
    assert calls["n"] == before and runner.calls == 1  # woken, but no git and no job
    clock.advance_ms(1100)  # the gap ends; the worker's own re-arm timer would find it...
    svc._wake.set()  # ...here we just do not wait out the real timer
    for _ in range(100):
        if runner.calls == 2:
            break
        await asyncio.sleep(0.01)
    assert runner.calls == 2 and calls["n"] > before
    stop.set()
    await asyncio.wait_for(task, 5)


async def test_revaluation_tracebacks_are_rate_limited(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    svc, _, runner, clock = make_service(tmp_path)
    runner.fail = RuntimeError("boom")
    await feed(svc)
    with caplog.at_level("ERROR", logger="exo.service"):
        for i in range(25):
            clock.advance_ms(11_000)
            await feed(svc, snap=False, mid_e9=(100 + i) * 10**9)
            await svc.revalue_pending()
    assert runner.calls == 25
    assert sum("revaluation of book" in r.message for r in caplog.records) == 10


async def test_exactly_one_ok_false_beat_even_if_the_heartbeat_timer_fires_during_it(
    tmp_path: Path,
) -> None:
    svc, bus, _, _ = make_service(tmp_path)
    await feed(svc)
    claimed = svc.state.claim_job()
    assert claimed is not None
    release = asyncio.Event()
    real_publish = bus.publish

    async def slow(subject: str, payload: bytes) -> None:
        if subject == "exo.health":
            await release.wait()
        await real_publish(subject, payload)

    bus.publish = slow  # type: ignore[method-assign]
    dying = asyncio.create_task(svc._die(claimed, "valuation hung"))
    await asyncio.sleep(0.01)
    await svc.heartbeat_tick()  # the 1 s timer firing while the final beat is in flight
    release.set()
    await dying
    beats = [live_pb2.HealthStatus.FromString(b) for s, b in bus.published if s == "exo.health"]
    assert len(beats) == 1 and not beats[0].ok and beats[0].detail["fatal"] == "valuation hung"
