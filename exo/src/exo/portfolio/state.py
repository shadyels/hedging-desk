"""`ExoState`: the pure, clock-injected decision logic of the EXO service loop (ADR-014).

Nothing here does I/O, sleeps, spawns a process or reads a clock except through the injected
`Clock`. The asyncio shell (`bus/service.py`) decodes messages, calls one method per message,
and publishes whatever `Out` list comes back. Everything the ADR calls a decision lives here:

* the per-book position ledger, bootstrap and reconciliation (s8): halt after `grace_ms` of
  divergence on accepted snapshots, un-halt only on a LATER accepted agreeing snapshot (user
  answer Q3: no auto re-seed; an EXO restart re-bootstraps), the health-only no-snapshot halt;
* consume-site validation of every inbound message (s2, s5-s8, s14): an invalid message is INERT
  (counted and logged, never seeding, applying, deduping or clearing anything);
* dedupe: a bounded FIFO on `msg_id`, unbounded business-key sets on `(exec_id, book_id)` and
  `cross_id` (s8);
* revaluation triggering (coalesced, one job in flight), banded target publication (s9), the
  interval republish (a FRESH `msg_id`, the same snapshot content, no repricing) and `exo.health`.

Two clocks: `mono_ns` drives every timer (grace, snapshot gap), so a wall-clock step cannot halt
or un-halt a book; `wall_ns` stamps `Meta.sent_ns` and `ValuationSnapshot.as_of_ns` (epoch UTC,
protocol rule 4), so D1's non-decreasing `as_of_ns` guard survives an EXO restart (s3).
"""

from __future__ import annotations

import logging
import time
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from google.protobuf.message import Message

from exo.bus.convert import (
    Cross,
    ExecFill,
    LimitAlert,
    Mark,
    Positions,
    cap_text,
    recon_alert_pb,
    target_position_pb,
    valuation_snapshot_pb,
)
from exo.bus.gen import common_pb2, live_pb2
from exo.config import ExoConfig
from exo.portfolio.aggregate import band_for, should_publish
from exo.portfolio.book import Book
from exo.portfolio.revalue import BookJob, BookResult
from exo.provenance import ValuationMetaFields, publish_allowed, publish_status, to_valuation_meta
from exo.refdata import Instrument, Universe

log = logging.getLogger("exo.state")

MSG_ID_FIFO_CAP = 65_536  # ADR-014 s8
MAX_KEY_BYTES = 64  # mirrors D1's `msg_id_ok` (d1-gateway-nats MAX_MSG_ID_BYTES)
_I64_MIN, _I64_MAX = -(2**63), 2**63 - 1
_NS_PER_MS = 1_000_000
_LOG_FIRST = 10  # log the first N rejections per kind, then every 1000th


class Clock(Protocol):
    def mono_ns(self) -> int: ...
    def wall_ns(self) -> int: ...


class SystemClock:
    def mono_ns(self) -> int:
        return time.monotonic_ns()

    def wall_ns(self) -> int:
        return time.time_ns()


@dataclass
class Out:
    """One outbound NATS message, already built. `undo` (set when the state behind it is committed)
    reverts that state; the shell calls it if the publish raises, so EXO never believes in a
    target that did not leave (a phantom `published` value would suppress later in-band targets)."""

    subject: str
    msg: Message
    undo: Callable[[], None] | None = field(default=None, compare=False)


@dataclass(frozen=True)
class ClaimedJob:
    """A revaluation handed to the shell: the picklable job plus the marks it was built from."""

    book_id: int
    job: BookJob
    marks_e9: Mapping[int, int]  # instrument_id -> mid_px_e9 used for this cycle


@dataclass(frozen=True)
class _Valued:
    """The latest computed valuation of a book (what the interval republish repeats)."""

    result: BookResult
    meta: ValuationMetaFields
    as_of_ns: int
    marks_e9: Mapping[int, int]


@dataclass
class _BookState:
    book: Book
    underlyings: dict[int, Instrument]
    ledger: dict[int, int] | None = None  # None until the first accepted snapshot seeds it
    last_snap_sent_ns: int | None = None
    last_accept_mono: int = 0
    div_since_mono: int | None = None  # first divergent accepted snapshot of the current episode
    halt: str | None = None  # "recon" | "no-snapshot"
    halt_iid: int | None = None  # instrument named in the ReconAlert, reused on clear
    dirty: bool = False
    last: _Valued | None = None
    published: dict[int, int] = field(default_factory=dict)  # iid -> last published target
    last_as_of_ns: int = 0
    last_start_mono: int = 0
    last_end_mono: int | None = None  # when the previous job ended (success or failure)
    failed_at_mono: int | None = None  # set while the latest attempt has failed (ADR-014 s15)
    fresh_mark: bool = False  # a mark changed since the last claim
    pending_bypass: bool = False  # an un-halt owes a band-bypassing target publication (s8, s15)
    last_error: str | None = None


class _SeenFifo:
    """Bounded FIFO set (`deque` + `set`). Evicting an id is safe: every state-changing message
    also has a business key or a `sent_ns` guard (ADR-014 s8)."""

    def __init__(self, cap: int) -> None:
        self._cap = cap
        self._order: deque[str] = deque()
        self._set: set[str] = set()

    def add(self, key: str) -> bool:
        """True if newly added, False if already present."""
        if key in self._set:
            return False
        self._order.append(key)
        self._set.add(key)
        if len(self._order) > self._cap:
            self._set.discard(self._order.popleft())
        return True


def _key_ok(key: str, *, allow_empty: bool = False) -> bool:
    n = len(key.encode("utf-8", "replace"))
    return n <= MAX_KEY_BYTES and (allow_empty or n > 0)


def _in_i64(v: int) -> bool:
    return _I64_MIN <= v <= _I64_MAX


def _clamp_i64(v: int) -> int:
    return max(_I64_MIN, min(_I64_MAX, v))


def _fmt_e9(v: int) -> str:
    return f"{v // 10**9}.{v % 10**9:09d}"


class ExoState:
    def __init__(
        self,
        cfg: ExoConfig,
        universe: Universe,
        books: Sequence[Book],
        clock: Clock,
        sha: str | None,
        *,
        msg_id_cap: int = MSG_ID_FIFO_CAP,
    ) -> None:
        self._cfg = cfg
        self._clock = clock
        self._sha = sha
        self._instruments = {i.instrument_id: i for i in universe.instruments}
        self._universe_books = {b.book_id for b in universe.books}
        self._books = {
            b.book_id: _BookState(b, {p.instrument.instrument_id: p.instrument for p in b.products})
            for b in books
        }
        self._marks: dict[int, int] = {}
        self._mark_sent: dict[int, int] = {}
        self._seen = _SeenFifo(msg_id_cap)
        # ponytail: msg_id FIFO bounded (65,536); the business-key sets are unbounded (they grow
        # only with booked activity). Ceiling: memory over weeks of uptime. Upgrade: persist and
        # window them with the ledger (P3.M2).
        self._exec_keys: set[tuple[str, int]] = set()
        self._cross_ids: set[str] = set()
        self._limits: dict[tuple[int, int], str] = {}
        self._in_flight: ClaimedJob | None = None
        self._fatal: str | None = None
        self._log_n: Counter[str] = Counter()
        self._min_gap_ns = int(cfg.valuation.min_revaluation_gap_s * 1e9)
        self._retry_ns = int(cfg.valuation.retry_after_s * 1e9)
        self._skew_ns = int(cfg.bus.max_clock_skew_s * 1e9)
        self.rejects: Counter[str] = Counter()
        self.max_position_lines = len(universe.instruments)

    # --- introspection for the shell / tests ---

    @property
    def book_ids(self) -> list[int]:
        return sorted(self._books)

    @property
    def underlying_ids(self) -> list[int]:
        return sorted({iid for bs in self._books.values() for iid in bs.underlyings})

    @property
    def in_flight(self) -> bool:
        return self._in_flight is not None

    def halt_of(self, book_id: int) -> str | None:
        return self._books[book_id].halt

    def ledger_of(self, book_id: int) -> dict[int, int] | None:
        ledger = self._books[book_id].ledger
        return None if ledger is None else dict(ledger)

    def set_sha(self, sha: str | None) -> None:
        """`None` means provenance is unavailable: publishing is refused (fail closed)."""
        self._sha = sha

    def publishing_allowed(self) -> bool:
        return self._sha is not None and publish_allowed(
            self._sha, self._cfg.provenance.allow_dirty
        )

    # --- rejection bookkeeping ---

    def note_reject(self, kind: str, why: str) -> None:
        """Count and (rate-limited) log an inert message. `why` is untrusted: logged via repr and
        capped (ADR-014 s14)."""
        self.rejects[kind] += 1
        n = self.rejects[kind]
        if n <= _LOG_FIRST or n % 1000 == 0:
            log.warning("rejected %s (#%d): %r", kind, n, cap_text(why))

    def should_log(self, kind: str) -> bool:
        """The note_reject pattern for accepted-but-noisy events: first N, then every 1000th."""
        self._log_n[kind] += 1
        n = self._log_n[kind]
        return n <= _LOG_FIRST or n % 1000 == 0

    def on_fatal(self, reason: str) -> None:
        """A condition EXO cannot recover from (a hung worker, ADR-014 s15): health stays
        `ok=false` with the reason until the process exits."""
        self._fatal = cap_text(reason)

    # --- marks (s2) ---

    def on_mark(self, subject_instrument: int | None, m: Mark) -> None:
        if not _key_ok(m.msg_id):
            return self.note_reject("mark", f"bad msg_id length {len(m.msg_id)}")
        if m.instrument_id not in self._instruments or subject_instrument != m.instrument_id:
            return self.note_reject(
                "mark", f"instrument {m.instrument_id} vs subject {subject_instrument}"
            )
        if m.sent_ns > self._clock.wall_ns() + self._skew_ns:
            # far-future sent_ns would latch the replay guard and lock real marks out (s15)
            return self.note_reject("mark_future", f"sent_ns {m.sent_ns} ahead of local time")
        if m.sent_ns < self._mark_sent.get(m.instrument_id, 0):
            self.rejects["mark_older"] += 1  # replay guard: dropped with a counter (s2)
            return None
        if not self._seen.add(m.msg_id):
            self.rejects["duplicate"] += 1
            return None
        self._mark_sent[m.instrument_id] = m.sent_ns
        changed = self._marks.get(m.instrument_id) != m.mid_px_e9
        self._marks[m.instrument_id] = m.mid_px_e9
        if changed:
            for bs in self._books.values():
                if m.instrument_id in bs.underlyings:
                    bs.dirty = bs.fresh_mark = True
        return None

    # --- fills and crosses (s5, s6) ---

    def on_exec_fill(
        self, subject_book: int | None, subject_instrument: int | None, f: ExecFill
    ) -> None:
        if not (
            _key_ok(f.msg_id) and _key_ok(f.exec_id) and _key_ok(f.cl_ord_id, allow_empty=True)
        ):
            return self.note_reject("exec", "bad dedupe-key length")
        if f.book_id not in self._books or f.book_id not in self._universe_books:
            return self.note_reject("exec", f"book {f.book_id} is not an EXO book")
        if f.instrument_id not in self._instruments:
            return self.note_reject("exec", f"unknown instrument {f.instrument_id}")
        if subject_book != f.book_id or subject_instrument != f.instrument_id:
            return self.note_reject("exec", f"subject {subject_book}.{subject_instrument} mismatch")
        if f.side not in (common_pb2.SIDE_BUY, common_pb2.SIDE_SELL):
            return self.note_reject("exec", f"unknown side {f.side}")
        if (
            f.status == live_pb2.ORD_STATUS_UNSPECIFIED
            or f.status not in live_pb2.OrdStatus.values()
        ):
            return self.note_reject("exec", f"unknown status {f.status}")
        if f.last_qty_e2 < 0:
            return self.note_reject("exec", f"negative last_qty_e2 {f.last_qty_e2}")
        if f.last_qty_e2 == 0:
            self.rejects["exec_not_a_fill"] += 1  # ADR-014 s5: a fill has last_qty_e2 > 0
            return None
        if not self._seen.add(f.msg_id):
            self.rejects["duplicate"] += 1
            return None
        key = (f.exec_id, f.book_id)
        if key in self._exec_keys:
            self.rejects["duplicate_exec"] += 1  # same business key, fresh envelope (s5)
            return None
        self._exec_keys.add(key)
        sign = 1 if f.side == common_pb2.SIDE_BUY else -1
        self._apply(f.book_id, f.instrument_id, sign * f.last_qty_e2)
        return None

    def on_cross(self, c: Cross) -> None:
        if not (_key_ok(c.msg_id) and _key_ok(c.cross_id)):
            return self.note_reject("cross", "bad dedupe-key length")
        if c.instrument_id not in self._instruments:
            return self.note_reject("cross", f"unknown instrument {c.instrument_id}")
        if (
            c.buy_book_id <= 0
            or c.sell_book_id <= 0
            or c.buy_book_id == c.sell_book_id
            or c.buy_book_id not in self._universe_books
            or c.sell_book_id not in self._universe_books
        ):
            return self.note_reject("cross", f"bad books {c.buy_book_id}/{c.sell_book_id}")
        if c.qty_e2 <= 0:
            return self.note_reject("cross", f"non-positive qty_e2 {c.qty_e2}")
        if c.buy_book_id not in self._books and c.sell_book_id not in self._books:
            return None  # firm-wide subject: not ours, and not recorded
        if not self._seen.add(c.msg_id):
            self.rejects["duplicate"] += 1
            return None
        if c.cross_id in self._cross_ids:
            self.rejects["duplicate_cross"] += 1
            return None
        self._cross_ids.add(c.cross_id)
        if c.buy_book_id in self._books:
            self._apply(c.buy_book_id, c.instrument_id, c.qty_e2)
        if c.sell_book_id in self._books:
            self._apply(c.sell_book_id, c.instrument_id, -c.qty_e2)
        return None

    def _apply(self, book_id: int, instrument_id: int, delta: int) -> None:
        ledger = self._books[book_id].ledger
        if ledger is None:
            # Before bootstrap the first snapshot already contains this fill (s8: quiescence).
            self.rejects["pre_bootstrap_ignored"] += 1
            return
        ledger[instrument_id] = ledger.get(instrument_id, 0) + delta  # exact; compared in recon

    # --- limit alerts (s12): logging and health only ---

    def on_limit_alert(self, a: LimitAlert) -> None:
        if not _key_ok(a.msg_id):
            return self.note_reject("limit", "bad msg_id length")
        if a.instrument_id not in self._instruments:
            return self.note_reject("limit", f"unknown instrument {a.instrument_id}")
        if a.greek not in (
            live_pb2.RiskLimitAlert.GREEK_VEGA,
            live_pb2.RiskLimitAlert.GREEK_GAMMA,
            live_pb2.RiskLimitAlert.GREEK_RHO,
        ):
            return self.note_reject("limit", f"unknown greek {a.greek}")
        if not self._seen.add(a.msg_id):
            self.rejects["duplicate"] += 1
            return None
        name = live_pb2.RiskLimitAlert.Greek.Name(a.greek)
        if self.should_log("limit_alert"):
            log.warning("D1 limit alert %s %s: %r", a.instrument_id, name, a.text)
        key = (a.instrument_id, a.greek)
        if a.cleared:
            self._limits.pop(key, None)
        else:
            self._limits[key] = "hard" if a.breached else "warn"
        return None

    # --- position snapshots and reconciliation (s8) ---

    def on_position_snapshot(self, subject_book: int | None, p: Positions) -> list[Out]:
        self._refresh_gaps()
        bs = self._books.get(p.book_id)
        if not _key_ok(p.msg_id):
            self.note_reject("positions", "bad msg_id length")
            return []
        if bs is None or subject_book != p.book_id:
            self.note_reject("positions", f"book {p.book_id} vs subject {subject_book}")
            return []
        lines: dict[int, int] = {}
        for ln in p.lines:
            if ln.instrument_id not in self._instruments or ln.instrument_id in lines:
                self.note_reject("positions", f"line instrument {ln.instrument_id}")
                return []
            lines[ln.instrument_id] = ln.qty_e2
        if p.sent_ns > self._clock.wall_ns() + self._skew_ns:
            self.note_reject("positions_future", f"sent_ns {p.sent_ns} ahead of local time")
            return []
        if bs.last_snap_sent_ns is not None and p.sent_ns < bs.last_snap_sent_ns:
            self.rejects["positions_older"] += 1  # replay guard: inert, cannot clear a halt
            return []
        if not self._seen.add(p.msg_id):
            self.rejects["duplicate"] += 1
            return []
        now = self._clock.mono_ns()
        bs.last_snap_sent_ns = p.sent_ns
        bs.last_accept_mono = now
        if bs.ledger is None:
            # ponytail: bootstrap assumes quiescence -- a fill or cross in flight across this
            # snapshot double-counts and halts the book (the safe direction). Upgrade: a booking
            # sequence number on the snapshot and the reports.
            bs.ledger = dict(lines)
            log.info("book %d bootstrapped from D1 snapshot: %r", bs.book.book_id, lines)
            return self._targets(bs)
        diffs = self._divergences(bs, lines)
        if not diffs:
            bs.div_since_mono = None
            return self._unhalt(bs, lines) if bs.halt is not None else []
        if bs.div_since_mono is None:
            bs.div_since_mono = now
        grace_ns = self._cfg.recon.grace_ms * _NS_PER_MS
        if bs.halt != "recon" and now - bs.div_since_mono >= grace_ns:
            iid, exo_view, d1_view = diffs[0]
            bs.halt, bs.halt_iid = "recon", iid
            log.error(
                "book %d HALTED (recon): instrument %d exo=%d d1=%d",
                bs.book.book_id,
                iid,
                exo_view,
                d1_view,
            )
            return [self._recon_out(bs, iid, exo_view, d1_view, halted=True)]
        return []

    def _divergences(self, bs: _BookState, lines: Mapping[int, int]) -> list[tuple[int, int, int]]:
        """(instrument, exo_view, d1_view) for every instrument outside tolerance, by id. A ledger
        value outside sint64 is a divergence on that instrument (s8 Arithmetic)."""
        assert bs.ledger is not None
        tol = self._cfg.recon.tolerance_qty_e2
        out: list[tuple[int, int, int]] = []
        for iid in sorted(bs.ledger.keys() | lines.keys()):
            exo_view, d1_view = bs.ledger.get(iid, 0), lines.get(iid, 0)
            if not _in_i64(exo_view) or abs(exo_view - d1_view) > tol:
                out.append((iid, exo_view, d1_view))
        return out

    def _unhalt(self, bs: _BookState, lines: Mapping[int, int]) -> list[Out]:
        # ponytail: a halt clears ONLY on a later accepted agreeing snapshot or an EXO restart (user
        # answer Q3; no auto re-seed from D1). Upgrade: operator re-seed, owner P3.M2.
        assert bs.ledger is not None
        was, iid = bs.halt, bs.halt_iid
        bs.halt, bs.halt_iid = None, None
        log.warning("book %d halt (%s) cleared by an agreeing snapshot", bs.book.book_id, was)
        outs: list[Out] = []
        if was == "recon" and iid is not None:
            outs.append(
                self._recon_out(bs, iid, bs.ledger.get(iid, 0), lines.get(iid, 0), halted=False)
            )
        bs.pending_bypass = True  # kept until a target publication actually goes out (s15)
        return outs + self._targets(bs)

    def _recon_out(
        self, bs: _BookState, iid: int, exo_view: int, d1_view: int, *, halted: bool
    ) -> Out:
        pb = recon_alert_pb(
            book_id=bs.book.book_id,
            instrument=self._instruments[iid],
            exo_view_qty_e2=_clamp_i64(exo_view),
            d1_view_qty_e2=_clamp_i64(d1_view),
            publishing_halted=halted,
            sent_ns=self._clock.wall_ns(),
        )
        return Out("exo.alerts.recon", pb)

    def _refresh_gaps(self) -> None:
        """No-snapshot halt (s8): health-only, on engage; clears on the next agreeing snapshot."""
        gap_ns = self._cfg.recon.max_snapshot_gap_ms * _NS_PER_MS
        now = self._clock.mono_ns()
        for bs in self._books.values():
            if bs.ledger is not None and bs.halt is None and now - bs.last_accept_mono >= gap_ns:
                bs.halt = "no-snapshot"
                log.error(
                    "book %d HALTED (no-snapshot for >= %d ms)",
                    bs.book.book_id,
                    self._cfg.recon.max_snapshot_gap_ms,
                )

    # --- revaluation (s9) ---

    def _valuable(self, bs: _BookState) -> bool:
        return all(iid in self._marks for iid in bs.underlyings)

    def has_work(self) -> bool:
        """Some book is dirty and fully marked and nothing is in flight. Timing (pacing, retry) and
        publish refusal are NOT considered here: see `claim_wait_s` / `claim_job`."""
        return self._in_flight is None and any(
            bs.dirty and self._valuable(bs) for bs in self._books.values()
        )

    def _ready_at(self, bs: _BookState) -> int:
        """Monotonic ns at which this (dirty) book may start a job: the pacing gap from the end of
        its previous job; after a failure also the retry timer, unless a mark has changed."""
        at = 0 if bs.last_end_mono is None else bs.last_end_mono + self._min_gap_ns
        if bs.failed_at_mono is not None and not bs.fresh_mark:
            at = max(at, bs.failed_at_mono + self._retry_ns)
        return at

    def _candidates(self) -> list[_BookState]:
        if self._in_flight is not None or not self.publishing_allowed():
            return []
        return [bs for bs in self._books.values() if bs.dirty and self._valuable(bs)]

    def claim_wait_s(self) -> float | None:
        """Seconds until some book may be claimed (0 = now); `None` if there is nothing to wait
        for (nothing dirty, a job in flight, or publishing refused)."""
        ready = [self._ready_at(bs) for bs in self._candidates()]
        if not ready:
            return None
        return max(0, min(ready) - self._clock.mono_ns()) / 1e9

    def claim_job(self) -> ClaimedJob | None:
        """Pick the dirty, fully-marked, paced-out book valued longest ago and start its job;
        `None` if one is in flight, nothing is claimable yet, or publishing is refused (the book
        stays dirty)."""
        sha, now = self._sha, self._clock.mono_ns()
        ready = [bs for bs in self._candidates() if self._ready_at(bs) <= now]
        if not ready or sha is None:
            return None
        bs = min(ready, key=lambda b: (b.last_start_mono, b.book.book_id))
        marks_e9 = {iid: self._marks[iid] for iid in bs.underlyings}
        symbols = {i.symbol: self._marks[iid] / 1e9 for iid, i in bs.underlyings.items()}
        # as_of_ns: epoch wall clock at revaluation START, never below this book's previous one.
        as_of = max(self._clock.wall_ns(), bs.last_as_of_ns)
        job = BookJob(
            book=bs.book,
            marks=symbols,
            params=self._cfg.models.params,
            scheme=self._cfg.models.defaults.scheme,
            n_steps_per_year=self._cfg.models.defaults.n_steps_per_year,
            bumps=self._cfg.greeks,
            git_sha=sha,
            created_ns=as_of,
        )
        bs.dirty = bs.fresh_mark = False
        bs.last_as_of_ns = as_of
        bs.last_start_mono = now
        self._in_flight = ClaimedJob(bs.book.book_id, job, marks_e9)
        return self._in_flight

    def on_valuation_failed(self, claimed: ClaimedJob, why: str) -> None:
        """Revaluation isolation (ADR-014 s15): nothing is published for this cycle, the loop
        continues, `exo.health` goes `ok=false` with the reason, and the book's interval republish
        stops (so D1's max-age WARN fires). The book stays dirty: it is retried after
        `retry_after_s`, or at once on a changed mark (the pacing gap always applies)."""
        bs = self._books[claimed.book_id]
        now = self._clock.mono_ns()
        self._in_flight = None
        bs.dirty = True
        bs.failed_at_mono = bs.last_end_mono = now
        bs.last_error = cap_text(why)
        self.note_reject("revaluation", why)

    def on_valuation_result(self, claimed: ClaimedJob, result: BookResult) -> list[Out]:
        """Publish the cycle's `ValuationSnapshot` (always, even when halted) and the banded
        targets. All wire messages are built before any state is committed, so a builder error
        leaves no half-published cycle."""
        self._refresh_gaps()
        bs = self._books[claimed.book_id]
        self._in_flight = None
        bs.last_end_mono = self._clock.mono_ns()
        if not self.publishing_allowed():
            bs.dirty = True  # recompute once the tree is clean / provenance is back
            return []
        valued = _Valued(
            result=result,
            meta=to_valuation_meta(result.manifest),
            as_of_ns=claimed.job.created_ns,
            marks_e9=claimed.marks_e9,
        )
        snapshot = self._snapshot_out(bs, valued)
        target_outs = self._plan_targets(bs, valued)
        bs.last, bs.last_error, bs.failed_at_mono = valued, None, None
        self._commit(bs, target_outs)
        return [snapshot, *(o for _, _, o in target_outs)]

    def _snapshot_out(self, bs: _BookState, v: _Valued) -> Out:
        pb = valuation_snapshot_pb(
            book_id=bs.book.book_id,
            as_of_ns=v.as_of_ns,
            sent_ns=self._clock.wall_ns(),
            valuation=v.meta,
            lines=v.result.lines,
        )
        return Out(f"exo.valuations.{bs.book.book_id}", pb)

    def on_interval(self) -> list[Out]:
        """Republish each healthy book's latest snapshot: same content and `as_of_ns`, FRESH
        `msg_id`, no repricing. Runs whether or not a revaluation is in flight (D1's max-age, s3).
        A book whose latest attempt failed is NOT republished until a valuation succeeds again
        (s15): D1's staleness WARN must fire rather than a stale number looking current.
        ponytail: a reprice here would be a no-op -- term sheets do not age between cycles
        (fixed seed, static book). Trigger: a calendar/day-count convention in refdata (the
        same trigger as products/tarf.py's fixing-date ponytail)."""
        self._refresh_gaps()
        if not self.publishing_allowed():
            return []
        return [
            self._snapshot_out(bs, bs.last)
            for bs in self._books.values()
            if bs.last is not None and bs.last_error is None
        ]

    # --- target publication (s9) ---

    def _targets(self, bs: _BookState) -> list[Out]:
        """Publish from the latest valuation on bootstrap / un-halt."""
        if bs.last is None:
            return []
        plan = self._plan_targets(bs, bs.last)
        self._commit(bs, plan)
        return [o for _, _, o in plan]

    def _plan_targets(self, bs: _BookState, v: _Valued) -> list[tuple[int, int, Out]]:
        """(instrument, new target, message) for every target that must go out now. Pure: nothing
        is committed until `_commit`."""
        if bs.ledger is None or bs.halt is not None or not self.publishing_allowed():
            return []
        plan: list[tuple[int, int, Out]] = []
        for iid in sorted(v.result.targets):
            new, last = v.result.targets[iid], bs.published.get(iid)
            instrument = self._instruments[iid]
            band = band_for(instrument, self._cfg.rehedge)
            if not should_publish(new, last, band, unhalted=bs.pending_bypass):
                continue
            pb = target_position_pb(
                book_id=bs.book.book_id,
                instrument=instrument,
                target_qty_e2=new,
                as_of_ns=v.as_of_ns,
                band_qty_e2=band,
                valuation=v.meta,
                reason=self._reason(v, iid, new, last, band, bs.pending_bypass),
                sent_ns=self._clock.wall_ns(),
            )
            plan.append((iid, new, Out(f"exo.targets.{bs.book.book_id}.{iid}", pb)))
        return plan

    def _commit(self, bs: _BookState, plan: list[tuple[int, int, Out]]) -> None:
        """Record planned targets as published, and clear `pending_bypass` (the first publication
        that actually goes out). Each message carries an `undo` for a failed publish."""
        if not plan:
            return
        bypass = bs.pending_bypass
        bs.pending_bypass = False
        for iid, new, out in plan:
            prev = bs.published.get(iid)
            bs.published[iid] = new

            def undo(
                bs: _BookState = bs, iid: int = iid, new: int = new, prev: int | None = prev
            ) -> None:
                if bs.published.get(iid) == new:  # a later publication must not be clobbered
                    if prev is None:
                        del bs.published[iid]
                    else:
                        bs.published[iid] = prev
                bs.pending_bypass = bs.pending_bypass or bypass

            out.undo = undo

    @staticmethod
    def _reason(v: _Valued, iid: int, new: int, last: int | None, band: int, bypass: bool) -> str:
        if bypass and last is not None:
            head = f"resume: halt cleared, latest target published (band {band:,})"
        elif last is None:
            head = f"initial: first target (band {band:,})"
        else:
            head = f"rehedge: |Δtarget|={abs(new - last):,} > band {band:,}"
        names = [
            n for ln in v.result.lines if ln.instrument.instrument_id == iid for n in ln.flagged
        ]
        flagged = ",".join(dict.fromkeys(names)) or "none"
        return f"{head}; mark {_fmt_e9(v.marks_e9[iid])}; flagged: {flagged}"

    # --- health (s12) ---

    def health(self, *, connected: bool) -> tuple[bool, dict[str, str]]:
        self._refresh_gaps()
        sha = self._sha
        detail: dict[str, str] = {
            "git_sha": sha or "unavailable",
            "publish": (
                publish_status(sha, self._cfg.provenance.allow_dirty)
                if sha is not None
                else "refused: git sha unavailable (provenance)"
            ),
            "valuation_in_flight": "true" if self._in_flight else "false",
            "nats": "connected" if connected else "disconnected",
            "rejected_total": str(sum(self.rejects.values())),
        }
        halted = failed = False
        for bid in sorted(self._books):
            bs = self._books[bid]
            if bs.ledger is None:
                state = "awaiting_snapshot"
            elif bs.halt is not None:
                state, halted = f"halted:{bs.halt}", True
            elif bs.last is None:
                state = "awaiting_marks"
            else:
                state = "publishing"
            detail[f"book.{bid}.state"] = state
            detail[f"book.{bid}.last_valuation_ns"] = str(bs.last.as_of_ns if bs.last else 0)
            if bs.last_error is not None:
                detail[f"book.{bid}.last_error"] = bs.last_error
                failed = True
        for (iid, greek), level in sorted(self._limits.items()):
            name = live_pb2.RiskLimitAlert.Greek.Name(greek).removeprefix("GREEK_").lower()
            detail[f"limit.{iid}.{name}"] = level
        if self._fatal is not None:
            detail["fatal"] = self._fatal
        ok = connected and self.publishing_allowed() and not (halted or failed or self._fatal)
        return ok, detail
