"""Shared fixtures for the P2.M4 slice-4 tests: a settable clock, a fabricated `BookResult` whose
targets the test controls, and message builders for the inbound side."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from weakref import WeakKeyDictionary

from _book_fixtures import book_doc, real_config, real_universe, storyline_product_doc

from exo.bus.convert import Cross, ExecFill, Mark, PositionLine, Positions
from exo.bus.gen import common_pb2, live_pb2
from exo.config import ExoConfig
from exo.greeks.bump import Estimate
from exo.portfolio.aggregate import LineValues
from exo.portfolio.book import Book, parse_book
from exo.portfolio.revalue import BookResult, model_id
from exo.portfolio.state import ClaimedJob, ExoState, Out
from exo.provenance import EngineSettings, RunManifest
from exo.refdata import Universe

CFG = real_config()
UNI = real_universe(CFG)
SHA = "a" * 40
BOOK = 3  # EXO-SP
AAPL, MSFT = 1001, 1002
E9 = 10**9

_MSFT_WARRANT: dict[str, Any] = {
    "product_id": "EXO-SP-W-0002", "type": "warrant_put", "desk_quantity": -10.0,
    "underlying": "MSFT", "strike": 420.0, "expiry": 1.0, "ratio": 1.0, "exercise": "european",
}  # fmt: skip


@dataclass
class FakeClock:
    mono: int = 10_000_000_000
    wall: int = 1_792_000_000_000_000_000

    def mono_ns(self) -> int:
        return self.mono

    def wall_ns(self) -> int:
        return self.wall

    def advance_ms(self, ms: int) -> None:
        self.mono += ms * 1_000_000
        self.wall += ms * 1_000_000


def book(*, two_underlyings: bool = False) -> Book:
    products = [storyline_product_doc()] + ([_MSFT_WARRANT] if two_underlyings else [])
    (b,) = parse_book(book_doc(*products, n_paths=1000), CFG, UNI)
    return b


_CLOCKS: WeakKeyDictionary[ExoState, FakeClock] = WeakKeyDictionary()


def make_state(
    clock: FakeClock | None = None,
    *,
    cfg: ExoConfig = CFG,
    sha: str | None = SHA,
    two_underlyings: bool = False,
    universe: Universe = UNI,
    msg_id_cap: int = 65_536,
) -> tuple[ExoState, FakeClock]:
    clock = clock or FakeClock()
    st = ExoState(
        cfg, universe, (book(two_underlyings=two_underlyings),), clock, sha, msg_id_cap=msg_id_cap
    )
    _CLOCKS[st] = clock
    return st, clock


def fake_result(
    b: Book, targets: dict[int, int], *, created_ns: int = 1, sha: str = SHA
) -> BookResult:
    est = Estimate(1.0, 0.1)
    lines = tuple(
        LineValues(
            product_id=p.product_id,
            product_type=p.product_type,
            instrument=p.instrument,
            pv=est, delta=est, gamma=est, vega=est, rho=est,
            div_sens=est, rho_foreign=Estimate(0.0, 0.0), theta=est,
            flagged=("delta", "gamma"),
        )
        for p in b.products
    )  # fmt: skip
    manifest = RunManifest(
        schema_version=3,
        run_id=f"run-{created_ns}",
        created_ns=created_ns,
        git_sha=sha,
        model_id=model_id("qe"),
        params_hash="f" * 64,
        seed=b.seed,
        n_paths=b.n_paths,
        engine=EngineSettings(scheme="qe", n_steps=0, antithetic=True, n_steps_per_year=252),
        params={"AAPL": {"s0": 187.5}},
    )
    return BookResult(lines=lines, targets=dict(targets), manifest=manifest)


_n = 0


def _mid() -> str:
    global _n
    _n += 1
    return f"msg-{_n}"


def mark(
    iid: int = AAPL, mid_e9: int = 187_500_000_000, sent_ns: int = 1, msg_id: str | None = None
) -> Mark:
    return Mark(msg_id if msg_id is not None else _mid(), iid, mid_e9, sent_ns, 0)


def snapshot(
    lines: dict[int, int] | None = None,
    *,
    book_id: int = BOOK,
    sent_ns: int = 1,
    msg_id: str | None = None,
) -> Positions:
    return Positions(
        msg_id if msg_id is not None else _mid(), book_id, sent_ns,
        tuple(PositionLine(i, q, 0) for i, q in (lines or {}).items()),
    )  # fmt: skip


def fill(
    qty: int = 10_000, *, side: int = common_pb2.SIDE_BUY, book_id: int = BOOK, iid: int = AAPL,
    exec_id: str = "ex-1", msg_id: str | None = None, status: int = live_pb2.ORD_STATUS_FILLED,
    cl_ord_id: str = "cl-1",
) -> ExecFill:  # fmt: skip
    return ExecFill(
        msg_id if msg_id is not None else _mid(),
        cl_ord_id,
        exec_id,
        book_id,
        iid,
        side,
        status,
        qty,
        1,
    )


def cross(
    qty: int = 10_000, *, cross_id: str = "x-1", buy: int = BOOK, sell: int = 1, iid: int = AAPL,
    msg_id: str | None = None,
) -> Cross:  # fmt: skip
    return Cross(msg_id if msg_id is not None else _mid(), cross_id, iid, buy, sell, qty, 1)


def feed_fill(st: ExoState, f: ExecFill) -> None:
    st.on_exec_fill(f.book_id, f.instrument_id, f)


def claim(st: ExoState) -> ClaimedJob | None:
    """`claim_job`, first waiting out the pacing / retry gates on the state's fake clock."""
    wait = st.claim_wait_s()
    if wait:
        _CLOCKS[st].advance_ms(int(wait * 1000) + 1)
    return st.claim_job()


def run_job(st: ExoState, targets: dict[int, int]) -> tuple[ClaimedJob, list[Out]]:
    """Claim the pending job and deliver a fabricated result for it."""
    claimed = claim(st)
    assert claimed is not None
    result = fake_result(
        claimed.job.book, targets, created_ns=claimed.job.created_ns, sha=claimed.job.git_sha
    )
    return claimed, st.on_valuation_result(claimed, result)


def subjects(outs: list[Out]) -> list[str]:
    return [o.subject for o in outs]
