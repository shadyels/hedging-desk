"""The EXO service loop: a thin asyncio shell around the pure `ExoState` (ADR-014).

The shell does only I/O: subscribe, decode (`bus/convert.py`), call ONE `ExoState` method per
message, publish what comes back, run the 1 s heartbeat and the interval-republish timer, and
run revaluations in a single-worker `ProcessPoolExecutor` so a heavy job cannot stall the bus
(exo/CLAUDE.md). Each timer is its own task, so the interval republish keeps firing while a
revaluation is in flight (D1's `valuation_max_age_s` is 15 s, ADR-014 s3).

`nats-py` ships `py.typed`, so no stub or mypy override is needed. The bus is a three-method
`Bus` Protocol so tests inject a fake and the real adapter is the only code that touches `nats`.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import multiprocessing
import signal
from collections.abc import Awaitable, Callable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Protocol

import nats
from google.protobuf.message import DecodeError
from nats.aio.client import Client as NatsClient
from nats.aio.msg import Msg

from exo.bus.convert import (
    cross_from_pb,
    exec_fill_from_pb,
    health_pb,
    limit_alert_from_pb,
    mark_from_pb,
    positions_from_pb,
)
from exo.bus.gen import live_pb2
from exo.config import EXO_ROOT, ExoConfig
from exo.portfolio.book import Book, load_book
from exo.portfolio.revalue import BookJob, BookResult, revalue_book
from exo.portfolio.state import ClaimedJob, Clock, ExoState, Out, SystemClock
from exo.provenance import ProvenanceError, RunManifest, git_sha
from exo.refdata import load_universe

log = logging.getLogger("exo.service")

Handler = Callable[[str, bytes], Awaitable[None]]
Runner = Callable[[BookJob], Awaitable[BookResult]]
ShaFn = Callable[[], str]


class Bus(Protocol):
    @property
    def connected(self) -> bool: ...
    async def publish(self, subject: str, payload: bytes) -> None: ...
    async def subscribe(self, subject: str, handler: Handler) -> None: ...


class NatsBus:
    """`Bus` over an already-connected `nats.aio.client.Client`."""

    def __init__(self, nc: NatsClient) -> None:
        self._nc = nc

    @property
    def connected(self) -> bool:
        return bool(self._nc.is_connected)

    async def publish(self, subject: str, payload: bytes) -> None:
        await self._nc.publish(subject, payload)

    async def subscribe(self, subject: str, handler: Handler) -> None:
        async def cb(msg: Msg) -> None:
            await handler(msg.subject, msg.data)

        await self._nc.subscribe(subject, cb=cb)


class PoolRunner:
    """Revaluations in a one-worker `spawn` pool (never fork out of a threaded asyncio process)."""

    def __init__(self) -> None:
        self._pool = self._new()

    @staticmethod
    def _new() -> ProcessPoolExecutor:
        return ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))

    async def __call__(self, job: BookJob) -> BookResult:
        try:
            return await asyncio.get_running_loop().run_in_executor(self._pool, revalue_book, job)
        except BrokenProcessPool:
            self._pool.shutdown(wait=False)
            self._pool = self._new()  # a dead worker must not wedge every later cycle
            raise

    def kill(self) -> None:
        """Terminate the worker process(es): a hung job cannot be cancelled any other way. SIGTERM,
        then SIGKILL after 2 s, so the exit-time join cannot hang on an unresponsive worker.
        ponytail: reaches into `ProcessPoolExecutor._processes` (private; may be None after
        `shutdown()` on 3.13, hence the guards). Upgrade: a pool wrapper that owns its `Process`
        objects."""
        procs = list((getattr(self._pool, "_processes", None) or {}).values())
        for proc in procs:
            with contextlib.suppress(Exception):
                proc.terminate()
        for proc in procs:
            with contextlib.suppress(Exception):
                proc.join(2)
                if proc.is_alive():
                    proc.kill()
        with contextlib.suppress(Exception):
            self._pool.shutdown(wait=False, cancel_futures=True)

    def shutdown(self) -> None:
        # ponytail: on NORMAL shutdown wait=False leaves a running job to finish in the background
        # (the exit-time join waits for it, bounded by one revaluation / `job_timeout_s`).
        # Upgrade: call `kill()` here too once a faster SIGTERM exit matters.
        self._pool.shutdown(wait=False, cancel_futures=True)


def _token(tok: str) -> int | None:
    """A canonical positive numeric subject token (ADR-014 s1), else `None`."""
    if tok.isascii() and tok.isdecimal() and str(int(tok)) == tok and int(tok) > 0:
        return int(tok)
    return None


class FatalError(RuntimeError):
    """EXO cannot continue (ADR-014 s15): `python -m exo` exits non-zero."""


class ExoService:
    def __init__(
        self,
        cfg: ExoConfig,
        bus: Bus,
        runner: Runner,
        *,
        state: ExoState,
        clock: Clock,
        sha_fn: ShaFn = git_sha,
    ) -> None:
        self._cfg = cfg
        self._bus = bus
        self._runner = runner
        self.state = state
        self._clock = clock
        self._sha_fn = sha_fn
        self._wake = asyncio.Event()
        self._fatal = asyncio.Event()
        self._dying = False

    def _wake_if_work(self) -> None:
        # Cheap (no git): wake the worker only if a job is pending and publishing is allowed. The
        # worker itself checks the pacing / retry gate BEFORE touching git, and re-arms from
        # `claim_wait_s()`, so a mark that arrives inside the gap is picked up when it ends.
        if self.state.claim_wait_s() is not None:
            self._wake.set()

    # --- inbound ---

    async def subscribe_all(self) -> None:
        subjects = [f"d1.marks.{iid}" for iid in self.state.underlying_ids]
        for bid in self.state.book_ids:
            subjects += [f"d1.positions.{bid}", f"d1.exec.{bid}.>"]
        subjects += ["d1.crosses", "d1.alerts.limits"]
        for subject in subjects:
            await self._bus.subscribe(subject, self.handle)

    async def handle(self, subject: str, data: bytes) -> None:
        """One inbound message. A bad payload or a handler bug is counted, never fatal."""
        try:
            outs = self._dispatch(subject, data)
        except (DecodeError, ValueError) as exc:
            self.state.note_reject("decode", f"{subject}: {exc!r}")
            return
        except Exception as exc:  # a handler bug must not kill the subscription
            self.state.note_reject("handler", f"{subject}: {exc!r}")  # rate-limited logging
            return
        self._wake_if_work()
        await self._publish(outs)

    def _dispatch(self, subject: str, data: bytes) -> list[Out]:
        st, parts = self.state, subject.split(".")
        if parts[:2] == ["d1", "marks"] and len(parts) == 3:
            st.on_mark(_token(parts[2]), mark_from_pb(live_pb2.MarkPrice.FromString(data)))
        elif parts[:2] == ["d1", "positions"] and len(parts) == 3:
            pb = live_pb2.PositionSnapshot.FromString(data)
            pos = positions_from_pb(pb, max_lines=st.max_position_lines)
            return st.on_position_snapshot(_token(parts[2]), pos)
        elif parts[:2] == ["d1", "exec"] and len(parts) == 4:
            rep = exec_fill_from_pb(live_pb2.ExecutionReport.FromString(data))
            st.on_exec_fill(_token(parts[2]), _token(parts[3]), rep)
        elif parts == ["d1", "crosses"]:
            st.on_cross(cross_from_pb(live_pb2.InternalCrossNotice.FromString(data)))
        elif parts == ["d1", "alerts", "limits"]:
            st.on_limit_alert(limit_alert_from_pb(live_pb2.RiskLimitAlert.FromString(data)))
        else:
            st.note_reject("subject", subject)
        return []

    # --- outbound ---

    async def _publish(self, outs: list[Out]) -> None:
        for out in outs:
            try:
                await self._bus.publish(out.subject, out.msg.SerializeToString())
            except Exception:
                # ponytail: no retry -- core NATS is at-most-once (ADR-001). A failed TARGET is
                # rolled back (`undo`) so EXO never believes in a target that did not leave; the
                # next valuation re-evaluates it against the band. A lost recon alert is not
                # replayed (the health `halted` state still shows it).
                log.exception("publish failed on %s", out.subject)
                if out.undo is not None:
                    out.undo()

    async def _refresh_sha(self) -> None:
        try:
            self.state.set_sha(await asyncio.to_thread(self._sha_fn))
        except ProvenanceError:
            log.exception("provenance unavailable; publishing refused")
            self.state.set_sha(None)  # fail closed

    # --- revaluation cycle ---

    async def revalue_pending(self) -> None:
        """Run every revaluation that may start now (one job in flight, per-book pacing and retry
        gates in `ExoState.claim_job`). A job over `job_timeout_s` is FATAL (ADR-014 s15)."""
        while self.state.claim_wait_s() == 0:  # claimable NOW: only then spend two git calls
            await self._refresh_sha()
            claimed = self.state.claim_job()
            if claimed is None:
                return  # refused, or paced out: the worker re-arms from `claim_wait_s`
            try:
                async with asyncio.timeout(self._cfg.valuation.job_timeout_s) as limit:
                    result = await self._runner(claimed.job)
                await asyncio.to_thread(self._write_manifest, result.manifest)
                outs = self.state.on_valuation_result(claimed, result)
            except Exception as exc:  # revaluation isolation: skip this cycle's publish
                if isinstance(exc, TimeoutError) and limit.expired():
                    await self._die(claimed, "valuation hung")
                    raise FatalError("valuation hung") from exc
                if self.state.should_log("revaluation_traceback"):  # first 10, then every 1000th
                    log.exception("revaluation of book %d failed", claimed.book_id)
                self.state.on_valuation_failed(claimed, repr(exc))
                outs = []
            await self._publish(outs)

    async def _die(self, claimed: ClaimedJob, reason: str) -> None:
        """Fatal: mark the book failed, publish ONE `ok=false` health with the reason, and tell
        `run` to stop. The pool is not recycled (a hung worker is killed at exit)."""
        self.state.on_valuation_failed(claimed, reason)
        self.state.on_fatal(reason)
        self._dying = True  # blocks the 1 s heartbeat timer BEFORE the final beat is awaited
        await self.heartbeat_tick(final=True)  # the only ok=false beat
        self._fatal.set()

    def _write_manifest(self, manifest: RunManifest) -> None:
        # ponytail: one file per real revaluation per book, never rotated. Upgrade: rotation or
        # retention in `manifest_dir`. Trigger: sustained uptime.
        # A number whose manifest cannot be written is not published (raises -> isolation above).
        directory = self._cfg.valuation.manifest_dir
        directory.mkdir(parents=True, exist_ok=True)
        manifest.write(directory / f"{manifest.run_id}.toml")

    async def _valuation_worker(self) -> None:
        while True:
            try:
                await self.revalue_pending()
            except FatalError:
                return  # `_die` already told `run`
            except Exception:
                log.exception("valuation worker error")
            wait = self.state.claim_wait_s()  # a paced-out / retrying book: wake when it may start
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), wait)
            self._wake.clear()

    # --- timers ---

    async def interval_tick(self) -> None:
        await self._refresh_sha()
        await self._publish(self.state.on_interval())
        self._wake_if_work()  # retries a book left dirty by a dirty-tree refusal

    async def heartbeat_tick(self, *, final: bool = False) -> None:
        if self._dying and not final:
            return  # the fatal health message is (being) sent
        ok, detail = self.state.health(connected=self._bus.connected)
        pb = health_pb(ok=ok, detail=detail, sent_ns=self._clock.wall_ns())
        await self._publish([Out("exo.health", pb)])

    @staticmethod
    async def _every(period_s: float, tick: Callable[[], Awaitable[None]]) -> None:
        while True:
            await asyncio.sleep(period_s)
            try:
                await tick()
            except Exception:
                log.exception("timer tick failed")

    async def run(self, stop: asyncio.Event) -> None:
        await self.subscribe_all()
        tasks = [
            asyncio.create_task(self._valuation_worker()),
            asyncio.create_task(
                self._every(self._cfg.valuation.revaluation_interval_s, self.interval_tick)
            ),
            asyncio.create_task(self._every(1.0, self.heartbeat_tick)),
        ]
        await self.heartbeat_tick()
        waiters = [asyncio.create_task(stop.wait()), asyncio.create_task(self._fatal.wait())]
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        for task in [*tasks, *waiters]:
            task.cancel()
        await asyncio.gather(*tasks, *waiters, return_exceptions=True)
        if self._fatal.is_set():
            raise FatalError("valuation hung")


def _warmup_job(cfg: ExoConfig, book: Book, sha: str, now_ns: int) -> BookJob:
    """A 4-path job at the model's own `s0`: imports numpy/scipy in the worker before the first
    real cycle without computing anything that is published."""
    symbols = {p.instrument.symbol for p in book.products}
    return BookJob(
        book=dataclasses.replace(book, n_paths=4),
        marks={s: cfg.models.params[s].s0 for s in symbols},
        params=cfg.models.params,
        scheme=cfg.models.defaults.scheme,
        n_steps_per_year=cfg.models.defaults.n_steps_per_year,
        bumps=cfg.greeks,
        git_sha=sha,
        created_ns=now_ns,
    )


async def main(cfg: ExoConfig, stop: asyncio.Event | None = None) -> None:
    """Run EXO until `stop` is set (SIGTERM/SIGINT when not supplied). Fails fast at startup on a
    bad book, missing provenance, a failed warm-up or an unreachable NATS."""
    universe = load_universe(cfg.valuation.refdata)
    books = load_book(cfg.valuation.book, cfg, universe)
    clock: Clock = SystemClock()
    sha = git_sha()
    runner = PoolRunner()
    if stop is None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
    nc: NatsClient | None = None
    try:
        # Warm-up BEFORE connecting: `exo.health` (and so `ok`) only exists once the worker is hot.
        await runner(_warmup_job(cfg, books[0], sha, clock.wall_ns()))
        nc = await nats.connect(
            cfg.bus.nats_url,
            name="exo",
            max_reconnect_attempts=-1,
            error_cb=_log_error,
            disconnected_cb=_log_event("disconnected"),
            reconnected_cb=_log_event("reconnected"),
        )
        state = ExoState(cfg, universe, books, clock, sha)
        service = ExoService(cfg, NatsBus(nc), runner, state=state, clock=clock)
        await service.run(stop)
    except FatalError:
        runner.kill()  # the hung worker would otherwise be joined at interpreter exit
        raise
    finally:
        try:
            if nc is not None and not nc.is_closed:
                try:
                    await nc.drain()  # flush pending publishes, then close
                except Exception:  # e.g. ConnectionReconnectingError during an outage
                    log.exception("nats drain failed; closing")
                    await nc.close()
        except Exception:
            log.exception("nats close failed")
        finally:
            runner.shutdown()


async def _log_error(exc: Exception) -> None:
    log.error("nats error: %r", exc)


def _log_event(name: str) -> Callable[[], Awaitable[None]]:
    async def cb() -> None:
        log.warning("nats %s", name)

    return cb


def default_config_path() -> Path:
    return EXO_ROOT / "exo.toml"
