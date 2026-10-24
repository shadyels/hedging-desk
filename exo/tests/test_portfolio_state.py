"""`portfolio/state.py`: the pure ExoState (ADR-014 s2, s5-s9, s12, s14), driven with a fake clock.

R/B/D/I ids follow the P2.M4 spec (section 5.4 plus the review revisions)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from _state_helpers import (
    AAPL,
    BOOK,
    CFG,
    MSFT,
    SHA,
    UNI,
    FakeClock,
    book,
    claim,
    cross,
    fake_result,
    feed_fill,
    fill,
    make_state,
    mark,
    run_job,
    snapshot,
    subjects,
)

from exo.bus.convert import LimitAlert
from exo.bus.gen import common_pb2, live_pb2
from exo.config import ExoConfig, ProvenanceConfig, ReconConfig
from exo.portfolio.revalue import BookResult
from exo.portfolio.state import ClaimedJob, ExoState, Out, SystemClock

TARGET = f"exo.targets.{BOOK}.{AAPL}"
VALUATION = f"exo.valuations.{BOOK}"
RECON = "exo.alerts.recon"


def pb(out: Out) -> Any:  # noqa: ANN401 - dynamic proto attribute access in assertions
    return out.msg


def cfg_with(**sections: object) -> ExoConfig:
    return CFG.model_copy(update=sections)


def recon_cfg(*, tol: int = 0, grace_ms: int = 1500, gap_ms: int = 5000) -> ExoConfig:
    return cfg_with(
        recon=ReconConfig(tolerance_qty_e2=tol, grace_ms=grace_ms, max_snapshot_gap_ms=gap_ms)
    )


def bootstrapped(
    cfg: ExoConfig = CFG, lines: dict[int, int] | None = None
) -> tuple[ExoState, FakeClock]:
    """State with marks, a first snapshot (sent_ns=1) and a published initial target."""
    st, clock = make_state(cfg=cfg)
    st.on_mark(AAPL, mark())
    assert st.on_position_snapshot(BOOK, snapshot(lines or {}, sent_ns=1)) == []
    _, outs = run_job(st, {AAPL: 1_000_000})
    assert subjects(outs) == [VALUATION, TARGET]
    return st, clock


def snap(
    st: ExoState, clock: FakeClock, lines: dict[int, int], sent_ns: int, advance_ms: int = 1000
) -> list[Out]:
    clock.advance_ms(advance_ms)
    return st.on_position_snapshot(BOOK, snapshot(lines, sent_ns=sent_ns))


# --- bootstrap and R1 ---


def test_r1_no_target_before_the_first_position_snapshot_then_bootstrap_publishes() -> None:
    st, _ = make_state()
    assert st.health(connected=True)[1][f"book.{BOOK}.state"] == "awaiting_snapshot"
    st.on_mark(AAPL, mark())
    _, outs = run_job(st, {AAPL: 1_000_000})
    assert subjects(outs) == [VALUATION]  # monitoring survives; no target yet
    outs = st.on_position_snapshot(BOOK, snapshot({AAPL: 500}))
    assert subjects(outs) == [TARGET]  # first snapshot seeds the ledger and releases the target
    t = pb(outs[0])
    assert t.target_qty_e2 == 1_000_000 and t.band_qty_e2 == 50_000
    assert t.reason.startswith("initial:") and "mark 187.500000000" in t.reason
    assert st.ledger_of(BOOK) == {AAPL: 500}
    assert st.health(connected=True)[1][f"book.{BOOK}.state"] == "publishing"


def test_bootstrap_with_no_valuation_yet_publishes_nothing_and_valuation_then_publishes() -> None:
    st, _ = make_state()
    assert st.on_position_snapshot(BOOK, snapshot()) == []
    assert st.health(connected=True)[1][f"book.{BOOK}.state"] == "awaiting_marks"
    st.on_mark(AAPL, mark())
    _, outs = run_job(st, {AAPL: -200})
    assert subjects(outs) == [VALUATION, TARGET]


# --- R2 ledger and dedupe ---


def test_r2_fills_and_crosses_update_the_ledger_with_signs() -> None:
    st, _ = bootstrapped(lines={AAPL: 1000})
    feed_fill(st, fill(300, exec_id="a"))
    feed_fill(st, fill(50, side=common_pb2.SIDE_SELL, exec_id="b"))
    st.on_cross(cross(70, cross_id="c1", buy=BOOK, sell=1))
    st.on_cross(cross(20, cross_id="c2", buy=2, sell=BOOK))
    assert st.ledger_of(BOOK) == {AAPL: 1000 + 300 - 50 + 70 - 20}


def test_r2_same_exec_id_and_book_under_a_fresh_msg_id_applies_once() -> None:
    st, _ = bootstrapped()
    feed_fill(st, fill(100, exec_id="e1", msg_id="m-a"))
    feed_fill(st, fill(100, exec_id="e1", msg_id="m-b"))
    assert st.ledger_of(BOOK) == {AAPL: 100}
    assert st.rejects["duplicate_exec"] == 1


def test_r2_same_cross_id_twice_applies_once() -> None:
    st, _ = bootstrapped()
    st.on_cross(cross(40, cross_id="c1", msg_id="m-a"))
    st.on_cross(cross(40, cross_id="c1", msg_id="m-b"))
    assert st.ledger_of(BOOK) == {AAPL: 40}
    assert st.rejects["duplicate_cross"] == 1


def test_r2_same_msg_id_twice_applies_once_even_with_a_different_business_key() -> None:
    st, _ = bootstrapped()
    feed_fill(st, fill(100, exec_id="e1", msg_id="same"))
    feed_fill(st, fill(100, exec_id="e2", msg_id="same"))
    assert st.ledger_of(BOOK) == {AAPL: 100}
    assert st.rejects["duplicate"] == 1


def test_r12_after_msg_id_fifo_eviction_a_resent_fill_is_still_deduped_by_business_key() -> None:
    st, _ = make_state(msg_id_cap=2)
    st.on_position_snapshot(BOOK, snapshot())
    feed_fill(st, fill(100, exec_id="e1", msg_id="m1"))
    feed_fill(st, fill(1, exec_id="e2", msg_id="m2"))
    feed_fill(st, fill(1, exec_id="e3", msg_id="m3"))  # evicts the snapshot's id and m1
    feed_fill(st, fill(100, exec_id="e1", msg_id="m1"))  # m1 is no longer in the FIFO
    assert st.ledger_of(BOOK) == {AAPL: 102}
    assert st.rejects["duplicate_exec"] == 1 and st.rejects["duplicate"] == 0


def test_cross_not_touching_an_exo_book_is_ignored_and_not_recorded() -> None:
    st, _ = bootstrapped()
    st.on_cross(cross(40, cross_id="c1", buy=1, sell=2))
    st.on_cross(cross(40, cross_id="c1", buy=BOOK, sell=1))  # same id later, now ours: applies
    assert st.ledger_of(BOOK) == {AAPL: 40}


def test_a_fill_before_bootstrap_is_ignored_but_its_key_is_remembered() -> None:
    st, _ = make_state()
    feed_fill(st, fill(100, exec_id="e1"))
    st.on_position_snapshot(BOOK, snapshot({AAPL: 100}))  # the snapshot already contains it
    feed_fill(st, fill(100, exec_id="e1"))  # redelivered after bootstrap
    assert st.ledger_of(BOOK) == {AAPL: 100}
    assert st.rejects["pre_bootstrap_ignored"] == 1


def test_a_non_fill_report_changes_nothing() -> None:
    st, _ = bootstrapped()
    feed_fill(st, fill(0, exec_id="e1"))
    assert st.ledger_of(BOOK) == {} and st.rejects["exec_not_a_fill"] == 1


# --- R3-R7, R10: reconciliation ---


def test_r3_divergence_shorter_than_grace_halts_nothing_and_alerts_nothing() -> None:
    st, clock = bootstrapped()
    assert snap(st, clock, {AAPL: 100}, 2) == []  # divergent since now
    assert snap(st, clock, {}, 3) == []  # agrees again: episode over
    assert snap(st, clock, {AAPL: 100}, 4, 1400) == []  # a NEW episode starts, 0 ms old
    assert st.halt_of(BOOK) is None and st.health(connected=True)[0]


def test_r4_divergence_for_grace_across_two_snapshots_halts_with_exactly_one_alert() -> None:
    st, clock = bootstrapped()
    assert snap(st, clock, {AAPL: 100}, 2) == []  # first divergent snapshot
    assert snap(st, clock, {AAPL: 100}, 3, 1499) == []  # 1499 ms < grace_ms
    outs = snap(st, clock, {AAPL: 100}, 4, 1)  # exactly grace_ms after the first
    assert subjects(outs) == [RECON]
    a = pb(outs[0])
    assert (a.book_id, a.instrument.instrument_id, a.publishing_halted) == (BOOK, AAPL, True)
    assert (a.exo_view_qty_e2, a.d1_view_qty_e2) == (0, 100)
    assert snap(st, clock, {AAPL: 100}, 5) == []  # no repeat alert
    assert st.halt_of(BOOK) == "recon"
    ok, detail = st.health(connected=True)
    assert not ok and detail[f"book.{BOOK}.state"] == "halted:recon"
    # later band-exceeding target suppressed, snapshot still emitted
    st.on_mark(AAPL, mark(mid_e9=190 * 10**9, sent_ns=2))
    _, outs = run_job(st, {AAPL: 9_000_000})
    assert subjects(outs) == [VALUATION]


def test_r5_a_later_agreeing_snapshot_clears_once_and_publishes_the_latest_target_in_band() -> None:
    st, clock = bootstrapped()
    for sent in (2, 3, 4):
        snap(st, clock, {AAPL: 100}, sent, 1000)
    assert st.halt_of(BOOK) == "recon"
    st.on_mark(AAPL, mark(mid_e9=190 * 10**9, sent_ns=2))
    run_job(st, {AAPL: 1_010_000})  # within band of the published 1_000_000: nothing while halted
    outs = snap(st, clock, {}, 5)
    assert subjects(outs) == [RECON, TARGET]
    a, t = pb(outs[0]), pb(outs[1])
    assert a.publishing_halted is False and a.exo_view_qty_e2 == a.d1_view_qty_e2 == 0
    assert t.target_qty_e2 == 1_010_000 and t.reason.startswith("resume:")  # bypassed the band
    assert snap(st, clock, {}, 6) == []  # exactly one clear
    assert st.health(connected=True)[0]


def test_r6_tolerance_boundary() -> None:
    st, clock = bootstrapped(recon_cfg(tol=5, grace_ms=0))
    assert snap(st, clock, {AAPL: 5}, 2) == []  # |diff| == tolerance: in tolerance
    assert st.halt_of(BOOK) is None
    assert subjects(snap(st, clock, {AAPL: 6}, 3)) == [RECON]  # tolerance + 1: divergent
    st2, clock2 = bootstrapped(recon_cfg(tol=5, grace_ms=0))
    assert subjects(snap(st2, clock2, {AAPL: -6}, 2)) == [RECON]  # symmetric


def test_r7_grace_zero_halts_on_the_first_divergent_snapshot() -> None:
    st, clock = bootstrapped(recon_cfg(grace_ms=0))
    assert subjects(snap(st, clock, {AAPL: 1}, 2, 0)) == [RECON]


def test_the_alert_names_the_first_divergent_instrument_by_id() -> None:
    st, clock = bootstrapped(recon_cfg(grace_ms=0))
    outs = snap(st, clock, {MSFT: 7, AAPL: 9}, 2)
    assert pb(outs[0]).instrument.instrument_id == AAPL


def test_r8_a_replayed_older_agreeing_snapshot_does_not_clear_a_halt() -> None:
    st, clock = bootstrapped()
    for sent in (10, 11, 12):
        snap(st, clock, {AAPL: 100}, sent)
    assert st.halt_of(BOOK) == "recon"
    assert snap(st, clock, {}, 5) == []  # agrees but sent_ns 5 < 12: replay guard
    assert st.halt_of(BOOK) == "recon" and st.rejects["positions_older"] == 1
    assert subjects(snap(st, clock, {}, 13)) == [RECON, TARGET]  # a LATER agreeing one clears


def test_r10_a_ledger_result_beyond_sint64_halts() -> None:
    st, clock = bootstrapped(recon_cfg(tol=100, grace_ms=0), lines={AAPL: 2**63 - 1})
    feed_fill(st, fill(10, exec_id="e1"))
    assert st.ledger_of(BOOK) == {AAPL: 2**63 + 9}  # exact, never wrapped
    # |diff| = 10 is inside the tolerance of 100: only the sint64 range check can halt this
    outs = snap(st, clock, {AAPL: 2**63 - 1}, 2)
    assert subjects(outs) == [RECON]
    assert pb(outs[0]).exo_view_qty_e2 == 2**63 - 1  # clamped onto the wire
    feed_fill(st, fill(10, side=common_pb2.SIDE_SELL, exec_id="e2"))  # back in range, agrees
    assert subjects(snap(st, clock, {AAPL: 2**63 - 1}, 3)) == [RECON, TARGET]


def test_fills_keep_updating_the_ledger_while_halted() -> None:
    st, clock = bootstrapped(recon_cfg(grace_ms=0))
    snap(st, clock, {AAPL: 100}, 2)
    assert st.halt_of(BOOK) == "recon"
    feed_fill(st, fill(100, exec_id="e1"))
    assert subjects(snap(st, clock, {AAPL: 100}, 3)) == [RECON, TARGET]  # now they agree


# --- R9: the no-snapshot halt ---


def test_r9_no_snapshot_halt_engages_at_the_gap_and_clears_health_only_with_band_bypass() -> None:
    st, clock = bootstrapped()
    clock.advance_ms(4999)
    assert st.health(connected=True)[1][f"book.{BOOK}.state"] == "publishing"
    clock.advance_ms(1)
    ok, detail = st.health(connected=True)
    assert not ok and detail[f"book.{BOOK}.state"] == "halted:no-snapshot"
    assert st.halt_of(BOOK) == "no-snapshot"
    st.on_mark(AAPL, mark(mid_e9=190 * 10**9, sent_ns=2))
    _, outs = run_job(st, {AAPL: 1_010_000})  # in band: and halted anyway
    assert subjects(outs) == [VALUATION]
    outs = st.on_position_snapshot(BOOK, snapshot({}, sent_ns=2))
    assert subjects(outs) == [TARGET]  # no ReconAlert on engage or clear: health only
    assert pb(outs[0]).target_qty_e2 == 1_010_000 and pb(outs[0]).reason.startswith("resume:")
    assert st.health(connected=True)[0]


def test_a_rejected_snapshot_does_not_count_as_accepted_for_the_gap() -> None:
    st, clock = bootstrapped()
    clock.advance_ms(5000)
    st.on_position_snapshot(BOOK, snapshot({AAPL: 1, 7777: 1}, sent_ns=9))  # unknown instrument
    assert st.halt_of(BOOK) == "no-snapshot"


def test_no_snapshot_halt_still_escalates_to_recon_on_persistent_divergence() -> None:
    st, clock = bootstrapped()
    clock.advance_ms(5000)
    st.health(connected=True)
    assert st.halt_of(BOOK) == "no-snapshot"
    assert snap(st, clock, {AAPL: 100}, 2, 0) == []  # divergent, still under grace: stays
    assert subjects(snap(st, clock, {AAPL: 100}, 3, 1500)) == [RECON]
    assert st.halt_of(BOOK) == "recon"


def test_a_book_that_never_bootstrapped_has_no_gap_halt() -> None:
    st, clock = make_state()
    clock.advance_ms(60_000)
    assert st.health(connected=True)[0]


# --- R11: marks ---


def test_r11_older_mark_is_ignored_equal_sent_ns_accepted() -> None:
    st, _ = make_state()
    st.on_mark(AAPL, mark(mid_e9=100 * 10**9, sent_ns=10))
    st.on_mark(AAPL, mark(mid_e9=101 * 10**9, sent_ns=9))
    assert st.rejects["mark_older"] == 1
    claimed = st.claim_job()
    assert claimed is not None and claimed.marks_e9[AAPL] == 100 * 10**9
    st.on_mark(AAPL, mark(mid_e9=102 * 10**9, sent_ns=10))  # equal: accepted
    assert st.has_work() is False  # a job is in flight; the book is dirty again
    st.on_valuation_failed(claimed, "x")
    assert st.has_work() is True


# --- revaluation triggering ---


def test_a_book_is_valuable_only_with_a_mark_for_every_underlying() -> None:
    st, _ = make_state(two_underlyings=True)
    st.on_mark(AAPL, mark())
    assert st.has_work() is False and st.claim_job() is None
    st.on_mark(MSFT, mark(MSFT, 420 * 10**9))
    claimed = st.claim_job()
    assert claimed is not None
    assert claimed.job.marks == {"AAPL": 187.5, "MSFT": 420.0}


def test_valuations_are_coalesced_one_in_flight_latest_marks_next() -> None:
    st, _ = make_state()
    st.on_mark(AAPL, mark(mid_e9=100 * 10**9, sent_ns=1))
    first = st.claim_job()
    assert first is not None and st.in_flight and st.claim_job() is None
    st.on_mark(AAPL, mark(mid_e9=101 * 10**9, sent_ns=2))
    st.on_mark(AAPL, mark(mid_e9=102 * 10**9, sent_ns=3))
    assert st.has_work() is False  # nothing starts while one is in flight
    st.on_valuation_result(first, run_result(first))
    second = claim(st)
    assert second is not None and second.marks_e9[AAPL] == 102 * 10**9
    st.on_valuation_result(second, run_result(second))
    assert st.has_work() is False and st.claim_job() is None


def test_an_unchanged_mid_does_not_dirty_the_book() -> None:
    st, _ = make_state()
    st.on_mark(AAPL, mark(sent_ns=1))
    claimed = st.claim_job()
    assert claimed is not None
    st.on_valuation_result(claimed, run_result(claimed))
    st.on_mark(AAPL, mark(sent_ns=2))  # keepalive: same mid, later sent_ns
    assert st.has_work() is False


def run_result(claimed: ClaimedJob) -> BookResult:
    return fake_result(
        claimed.job.book,
        {AAPL: 1_000_000},
        created_ns=claimed.job.created_ns,
        sha=claimed.job.git_sha,
    )


def test_a_failed_revaluation_publishes_nothing_goes_unhealthy_and_stops_the_republish() -> None:
    st, clock = bootstrapped()
    assert len(st.on_interval()) == 1  # healthy: republished
    st.on_mark(AAPL, mark(mid_e9=190 * 10**9, sent_ns=2))
    claimed = claim(st)
    assert claimed is not None
    st.on_valuation_failed(claimed, "ValueError('bad mark')")
    assert st.in_flight is False
    ok, detail = st.health(connected=True)
    assert not ok and "bad mark" in detail[f"book.{BOOK}.last_error"]
    assert st.on_interval() == []  # D1's max-age WARN must be allowed to fire
    # a later success resumes the republish and health
    st.on_mark(AAPL, mark(mid_e9=191 * 10**9, sent_ns=3))
    _, outs = run_job(st, {AAPL: 1_000_000})
    assert subjects(outs) == [VALUATION]
    assert st.health(connected=True)[0] and len(st.on_interval()) == 1


def test_retry_gate_fires_on_the_retry_timer_or_on_a_changed_mark() -> None:
    st, clock = bootstrapped()
    st.on_mark(AAPL, mark(mid_e9=190 * 10**9, sent_ns=2))
    claimed = claim(st)
    assert claimed is not None
    st.on_valuation_failed(claimed, "boom")
    assert st.has_work()  # still dirty
    # no new mark: not claimable until retry_after_s (10 s) after the failure
    clock.advance_ms(9_999)
    assert st.claim_job() is None and 0 < (st.claim_wait_s() or 0) <= 0.001
    clock.advance_ms(1)
    assert st.claim_job() is not None  # timer fired
    # a changed mark releases it as soon as the pacing gap (1 s) allows
    st2, clock2 = bootstrapped()
    st2.on_mark(AAPL, mark(mid_e9=190 * 10**9, sent_ns=2))
    failed = claim(st2)
    assert failed is not None
    st2.on_valuation_failed(failed, "boom")
    st2.on_mark(AAPL, mark(mid_e9=191 * 10**9, sent_ns=3))
    clock2.advance_ms(999)
    assert st2.claim_job() is None  # min gap still applies
    clock2.advance_ms(1)
    assert st2.claim_job() is not None


def test_min_revaluation_gap_paces_a_book_from_the_end_of_its_previous_job() -> None:
    st, clock = bootstrapped()
    st.on_mark(AAPL, mark(mid_e9=190 * 10**9, sent_ns=2))
    assert st.has_work() and st.claim_job() is None  # job ended 0 ms ago
    assert st.claim_wait_s() == 1.0
    clock.advance_ms(600)
    st.on_mark(AAPL, mark(mid_e9=191 * 10**9, sent_ns=3))  # coalesces
    clock.advance_ms(399)
    assert st.claim_job() is None
    clock.advance_ms(1)
    claimed = st.claim_job()
    assert claimed is not None and claimed.marks_e9[AAPL] == 191 * 10**9
    # the gap runs from the END, not the start: a slow job does not shorten it
    clock.advance_ms(30_000)
    st.on_mark(AAPL, mark(mid_e9=192 * 10**9, sent_ns=4))
    st.on_valuation_result(claimed, run_result(claimed))
    st.on_mark(AAPL, mark(mid_e9=193 * 10**9, sent_ns=5))
    assert st.claim_job() is None


def test_far_future_marks_are_rejected_and_do_not_latch_the_replay_guard() -> None:
    st, clock = make_state()
    far = clock.wall + 61 * 10**9  # skew limit is 60 s
    st.on_mark(AAPL, mark(sent_ns=far))
    assert st.rejects["mark_future"] == 1 and not st.has_work()
    st.on_mark(AAPL, mark(sent_ns=clock.wall + 59 * 10**9))  # inside the limit
    assert st.has_work()
    st.on_mark(AAPL, mark(mid_e9=1, sent_ns=clock.wall))  # older than the last, not the future one
    assert st.rejects["mark_older"] == 1


def test_far_future_position_snapshots_are_rejected_and_do_not_latch() -> None:
    st, clock = make_state()
    assert st.on_position_snapshot(BOOK, snapshot({}, sent_ns=clock.wall + 61 * 10**9)) == []
    assert st.rejects["positions_future"] == 1 and st.ledger_of(BOOK) is None
    st.on_position_snapshot(BOOK, snapshot({AAPL: 5}, sent_ns=clock.wall))  # a normal one seeds
    assert st.ledger_of(BOOK) == {AAPL: 5}
    st.on_position_snapshot(BOOK, snapshot({AAPL: 5}, sent_ns=clock.wall + 3_600 * 10**9))
    assert st.rejects["positions_future"] == 2
    assert st.on_position_snapshot(BOOK, snapshot({AAPL: 5}, sent_ns=clock.wall + 1)) == []
    assert st.rejects["positions_older"] == 0  # the rejected future one never became the guard


def test_pending_bypass_survives_a_refused_publish_and_is_spent_by_the_next_one() -> None:
    st, clock = bootstrapped(recon_cfg(grace_ms=0))
    snap(st, clock, {AAPL: 100}, 2)
    assert st.halt_of(BOOK) == "recon"
    st.set_sha(SHA + "-dirty")  # publishing refused when the un-halt arrives
    outs = snap(st, clock, {}, 3)
    assert subjects(outs) == [RECON]  # the clear alert, but no (bypass) target
    st.set_sha(SHA)
    st.on_mark(AAPL, mark(mid_e9=190 * 10**9, sent_ns=2))
    _, outs = run_job(st, {AAPL: 1_010_000})  # in band of the published 1_000_000
    t = pb(outs[1])
    assert subjects(outs) == [VALUATION, TARGET] and t.reason.startswith("resume:")
    st.on_mark(AAPL, mark(mid_e9=191 * 10**9, sent_ns=3))
    _, outs = run_job(st, {AAPL: 1_020_000})
    assert subjects(outs) == [VALUATION]  # spent: back to the normal band


def test_pending_bypass_survives_a_failed_valuation() -> None:
    st, clock = bootstrapped(recon_cfg(grace_ms=0))
    snap(st, clock, {AAPL: 100}, 2)
    st.on_mark(AAPL, mark(mid_e9=190 * 10**9, sent_ns=2))
    claimed = claim(st)
    assert claimed is not None
    st.on_valuation_failed(claimed, "boom")
    outs = snap(st, clock, {}, 3)  # un-halt: a target from the last good valuation goes out
    assert subjects(outs) == [RECON, TARGET]
    st2, clock2 = bootstrapped(recon_cfg(grace_ms=0))
    snap(st2, clock2, {AAPL: 100}, 2)
    st2.on_mark(AAPL, mark(mid_e9=190 * 10**9, sent_ns=2))
    failed = claim(st2)
    assert failed is not None
    st2.on_valuation_failed(failed, "boom")
    st2.set_sha(SHA + "-dirty")
    snap(st2, clock2, {}, 3)  # nothing can go out: the bypass is still owed
    st2.set_sha(SHA)
    st2.on_mark(AAPL, mark(mid_e9=191 * 10**9, sent_ns=3))
    _, outs = run_job(st2, {AAPL: 1_010_000})
    assert pb(outs[1]).reason.startswith("resume:")


def test_an_owed_bypass_with_no_prior_target_is_worded_initial_not_resume() -> None:
    st, clock = make_state(cfg=recon_cfg(grace_ms=0))
    st.on_position_snapshot(BOOK, snapshot({}, sent_ns=1))
    snap(st, clock, {AAPL: 100}, 2)  # halted before any valuation exists
    assert snap(st, clock, {}, 3)[0].subject == RECON  # cleared: nothing to publish yet
    st.on_mark(AAPL, mark())
    _, outs = run_job(st, {AAPL: 1_000_000})
    assert subjects(outs) == [VALUATION, TARGET] and pb(outs[1]).reason.startswith("initial:")


def test_a_failed_bypass_publish_re_arms_the_bypass() -> None:
    st, clock = bootstrapped(recon_cfg(grace_ms=0))
    snap(st, clock, {AAPL: 100}, 2)
    outs = snap(st, clock, {}, 3)
    assert subjects(outs) == [RECON, TARGET] and outs[1].undo is not None
    outs[1].undo()  # the bypass target never left the process
    st.on_mark(AAPL, mark(mid_e9=190 * 10**9, sent_ns=2))
    _, outs = run_job(st, {AAPL: 1_010_000})  # in band: only the owed bypass can publish it
    assert subjects(outs) == [VALUATION, TARGET] and pb(outs[1]).reason.startswith("resume:")


def test_a_failed_publish_is_rolled_back_so_the_target_is_not_believed_published() -> None:
    st, _ = make_state()
    st.on_mark(AAPL, mark())
    st.on_position_snapshot(BOOK, snapshot())
    _, outs = run_job(st, {AAPL: 1_000_000})
    assert subjects(outs) == [VALUATION, TARGET] and outs[1].undo is not None
    outs[1].undo()  # the shell calls this when the publish raised
    st.on_mark(AAPL, mark(mid_e9=190 * 10**9, sent_ns=2))
    _, outs = run_job(st, {AAPL: 1_010_000})  # in band of the phantom value, but nothing went out
    assert subjects(outs) == [VALUATION, TARGET] and pb(outs[1]).reason.startswith("initial:")


def test_a_fatal_condition_keeps_health_down_with_the_reason() -> None:
    st, _ = make_state()
    st.on_fatal("valuation hung")
    ok, detail = st.health(connected=True)
    assert not ok and detail["fatal"] == "valuation hung"


# --- B1: band ---


def test_b1_band_gates_against_the_last_published_target_and_the_reason_names_it() -> None:
    st, _ = bootstrapped()
    for i, (new, expect_pub) in enumerate(
        [(1_040_000, False), (1_050_000, False), (1_060_000, True)]
    ):
        st.on_mark(AAPL, mark(mid_e9=(190 + i) * 10**9, sent_ns=10 + i))
        _, outs = run_job(st, {AAPL: new})
        assert subjects(outs) == ([VALUATION, TARGET] if expect_pub else [VALUATION]), new
    t = pb(outs[1])
    assert t.target_qty_e2 == 1_060_000 and t.band_qty_e2 == 50_000
    assert "|Δtarget|=60,000 > band 50,000" in t.reason
    assert "mark 192.000000000" in t.reason and "flagged: delta,gamma" in t.reason
    # the band is measured from the published 1_060_000 now
    st.on_mark(AAPL, mark(mid_e9=195 * 10**9, sent_ns=20))
    _, outs = run_job(st, {AAPL: 1_000_000})
    assert subjects(outs) == [VALUATION, TARGET]


def test_targets_and_snapshot_carry_the_cycles_provenance() -> None:
    st, _ = bootstrapped()
    st.on_mark(AAPL, mark(mid_e9=190 * 10**9, sent_ns=2))
    _, outs = run_job(st, {AAPL: 2_000_000})
    snap_pb, tgt = pb(outs[0]), pb(outs[1])
    assert snap_pb.valuation == tgt.valuation
    assert tgt.valuation.git_sha == SHA and tgt.valuation.n_paths == 1000
    assert tgt.as_of_ns == snap_pb.as_of_ns


def test_every_target_carries_book_id_and_the_full_valuation_meta() -> None:
    st, _ = bootstrapped()
    st.on_mark(AAPL, mark(mid_e9=190 * 10**9, sent_ns=2))
    _, outs = run_job(st, {AAPL: 2_000_000})
    tgt = pb(outs[1])
    assert tgt.book_id == BOOK and tgt.instrument.instrument_id == AAPL
    v = tgt.valuation
    assert v.model_id and v.params_hash == "f" * 64 and v.git_sha == SHA
    assert v.seed == book().seed and v.n_paths == 1000
    assert pb(outs[0]).book_id == BOOK


def test_restart_after_a_halt_rebootstraps_from_the_first_accepted_snapshot() -> None:
    st, clock = bootstrapped(recon_cfg(grace_ms=0))
    snap(st, clock, {AAPL: 100}, 2)
    assert st.halt_of(BOOK) == "recon"
    st2, _ = make_state(cfg=recon_cfg(grace_ms=0))
    assert st2.halt_of(BOOK) is None  # a halt is process-local: restart clears it
    st2.on_mark(AAPL, mark())
    assert st2.on_position_snapshot(BOOK, snapshot({AAPL: 100}, sent_ns=1)) == []  # no compare
    _, outs = run_job(st2, {AAPL: 1_000_000})
    assert subjects(outs) == [VALUATION, TARGET]
    assert st2.ledger_of(BOOK) == {AAPL: 100} and st2.halt_of(BOOK) is None


# --- D1: dirty tree ---


def test_d1_dirty_tree_refuses_to_publish_anything_and_health_says_so() -> None:
    st, _ = make_state(sha=SHA + "-dirty")
    st.on_mark(AAPL, mark())
    st.on_position_snapshot(BOOK, snapshot())
    assert st.has_work() and st.claim_job() is None  # no compute either; stays dirty
    assert st.on_interval() == []
    ok, detail = st.health(connected=True)
    assert not ok and detail["publish"].startswith("refused: dirty tree")
    assert detail["git_sha"] == SHA + "-dirty"
    st.set_sha(SHA)  # the tree became clean
    assert st.claim_job() is not None


def test_d1_a_result_that_lands_after_the_tree_went_dirty_is_dropped_and_recomputed() -> None:
    st, _ = make_state()
    st.on_mark(AAPL, mark())
    st.on_position_snapshot(BOOK, snapshot())
    claimed = st.claim_job()
    assert claimed is not None
    st.set_sha(SHA + "-dirty")
    assert st.on_valuation_result(claimed, run_result(claimed)) == []
    st.set_sha(SHA)
    assert st.has_work()


def test_d1_allow_dirty_publishes_and_the_wire_sha_ends_in_dirty() -> None:
    cfg = cfg_with(provenance=ProvenanceConfig(allow_dirty=True))
    st, _ = make_state(cfg=cfg, sha=SHA + "-dirty")
    st.on_mark(AAPL, mark())
    st.on_position_snapshot(BOOK, snapshot())
    _, outs = run_job(st, {AAPL: 1_000_000})
    assert subjects(outs) == [VALUATION, TARGET]
    assert all(pb(o).valuation.git_sha.endswith("-dirty") for o in outs)
    ok, detail = st.health(connected=True)
    assert ok and detail["publish"] == "enabled"


def test_unavailable_provenance_fails_closed() -> None:
    st, _ = make_state(sha=None)
    st.on_mark(AAPL, mark())
    assert st.claim_job() is None
    ok, detail = st.health(connected=True)
    assert not ok and detail["publish"].startswith("refused")


# --- I1 / R13: the interval republish ---


def test_i1_interval_republish_has_the_same_content_and_as_of_with_a_fresh_msg_id() -> None:
    st, clock = bootstrapped()
    st.on_mark(AAPL, mark(mid_e9=190 * 10**9, sent_ns=2))
    _, outs = run_job(st, {AAPL: 2_000_000})
    original = pb(outs[0])
    clock.advance_ms(5000)
    (re1,) = st.on_interval()
    clock.advance_ms(5000)
    (re2,) = st.on_interval()
    assert re1.subject == VALUATION
    r1, r2 = pb(re1), pb(re2)
    for r in (r1, r2):
        assert r.as_of_ns == original.as_of_ns
        assert r.valuation == original.valuation and r.lines == original.lines
        assert r.book_pv_e9 == original.book_pv_e9
        assert r.meta.sent_ns > original.meta.sent_ns
    assert len({original.meta.msg_id, r1.meta.msg_id, r2.meta.msg_id}) == 3
    assert not st.has_work()  # no reprice scheduled


def test_i1_nothing_to_republish_before_the_first_valuation() -> None:
    st, _ = make_state()
    assert st.on_interval() == []


def test_r13_state_republishes_while_a_valuation_is_in_flight() -> None:
    st, _ = bootstrapped()
    st.on_mark(AAPL, mark(mid_e9=190 * 10**9, sent_ns=2))
    claimed = claim(st)
    assert claimed is not None and st.in_flight
    assert subjects(st.on_interval()) == [VALUATION]
    assert st.in_flight  # unaffected


def test_a_halted_book_still_republishes_its_snapshot() -> None:
    st, clock = bootstrapped(recon_cfg(grace_ms=0))
    snap(st, clock, {AAPL: 100}, 2)
    assert subjects(st.on_interval()) == [VALUATION]


def test_as_of_is_epoch_wall_clock_at_start_and_a_restarted_state_is_not_behind() -> None:
    st1, clock1 = make_state()
    st1.on_mark(AAPL, mark())
    c1 = st1.claim_job()
    assert c1 is not None and c1.job.created_ns == clock1.wall
    clock2 = FakeClock(
        mono=1, wall=clock1.wall + 3_000_000_000
    )  # restart: mono resets, wall moves on
    st2, _ = make_state(clock2)
    st2.on_mark(AAPL, mark())
    c2 = st2.claim_job()
    assert c2 is not None and c2.job.created_ns >= c1.job.created_ns


def test_as_of_never_goes_backwards_within_a_process_on_a_wall_clock_step() -> None:
    st, clock = make_state()
    st.on_mark(AAPL, mark(sent_ns=1))
    c1 = st.claim_job()
    assert c1 is not None
    st.on_valuation_result(c1, run_result(c1))
    clock.wall -= 60_000_000_000
    st.on_mark(AAPL, mark(mid_e9=1, sent_ns=2))
    c2 = claim(st)
    assert c2 is not None and c2.job.created_ns == c1.job.created_ns


def test_system_clock_is_epoch_and_monotonic() -> None:
    c = SystemClock()
    assert c.wall_ns() > 1_700_000_000 * 10**9 and c.mono_ns() <= c.mono_ns()


# --- consume-site validation: rejected messages are inert ---

LONG = "x" * 65


@pytest.mark.parametrize(
    "case",
    [
        ("unknown_instrument", lambda st: st.on_mark(9999, mark(9999))),
        ("zero_id", lambda st: st.on_mark(0, mark(0))),
        ("subject_mismatch", lambda st: st.on_mark(MSFT, mark(AAPL))),
        ("long_msg_id", lambda st: st.on_mark(AAPL, mark(msg_id=LONG))),
        ("empty_msg_id", lambda st: st.on_mark(AAPL, mark(msg_id=""))),
    ],
    ids=lambda c: c[0],
)
def test_rejected_marks_are_inert(case: tuple[str, Callable[[ExoState], None]]) -> None:
    st, _ = make_state()
    case[1](st)
    assert st.rejects["mark"] == 1 and not st.has_work()


@pytest.mark.parametrize(
    "bad",
    [
        {"iid": 9999},
        {"book_id": 99},
        {"book_id": 4},  # a universe book that is not one of EXO's (book.toml has EXO-SP only)
        {"book_id": 0},
        {"side": 7},
        {"side": common_pb2.SIDE_UNSPECIFIED},
        {"status": 99},
        {"status": live_pb2.ORD_STATUS_UNSPECIFIED},
        {"msg_id": LONG},
        {"exec_id": LONG},
        {"exec_id": ""},
        {"cl_ord_id": LONG},
        {"qty": -5},
    ],
    ids=lambda d: ",".join(f"{k}={str(v)[:6]}" for k, v in d.items()),
)
def test_rejected_fills_are_inert_and_do_not_burn_their_keys(bad: dict[str, Any]) -> None:
    st, _ = bootstrapped(lines={AAPL: 7})
    args: dict[str, Any] = {"msg_id": "m-ok", "exec_id": "e-ok", **bad}
    f = fill(**args)
    st.on_exec_fill(f.book_id, f.instrument_id, f)
    assert st.ledger_of(BOOK) == {AAPL: 7} and sum(st.rejects.values()) == 1
    good = fill(10, msg_id="m-ok", exec_id="e-ok")  # same keys, valid: applies (nothing was burnt)
    feed_fill(st, good)
    assert st.ledger_of(BOOK) == {AAPL: 17}


def test_fill_subject_must_match_the_payload() -> None:
    st, _ = bootstrapped()
    f = fill(10)
    st.on_exec_fill(BOOK, MSFT, f)
    st.on_exec_fill(None, AAPL, f)
    assert st.ledger_of(BOOK) == {} and st.rejects["exec"] == 2


@pytest.mark.parametrize(
    "bad",
    [
        {"iid": 9999},
        {"buy": 0},
        {"sell": -1},
        {"buy": 99},
        {"sell": BOOK},  # buy == sell
        {"cross_id": LONG},
        {"cross_id": ""},
        {"msg_id": LONG},
        {"qty": 0},
        {"qty": -3},
    ],
    ids=lambda d: ",".join(f"{k}={str(v)[:6]}" for k, v in d.items()),
)
def test_rejected_crosses_are_inert(bad: dict[str, Any]) -> None:
    st, _ = bootstrapped(lines={AAPL: 7})
    st.on_cross(cross(**{"qty": 5, **bad}))
    assert st.ledger_of(BOOK) == {AAPL: 7} and st.rejects["cross"] == 1
    st.on_cross(cross(5))  # a valid cross with the default keys still applies
    assert st.ledger_of(BOOK) == {AAPL: 12}


@pytest.mark.parametrize(
    "lines,book_id,subject,msg_id",
    [
        ({"dup": True}, BOOK, BOOK, "m1"),
        ({7777: 1}, BOOK, BOOK, "m1"),
        ({}, BOOK, MSFT, "m1"),
        ({}, BOOK, None, "m1"),
        ({}, 99, 99, "m1"),
        ({}, 4, 4, "m1"),
        ({}, BOOK, BOOK, LONG),
    ],
)
def test_rejected_snapshots_neither_seed_nor_compare_nor_clear(
    lines: dict[Any, int], book_id: int, subject: int | None, msg_id: str
) -> None:
    from exo.bus.convert import PositionLine, Positions

    st, clock = make_state()
    if lines == {"dup": True}:
        snap_bad = Positions(
            msg_id, book_id, 1, (PositionLine(AAPL, 1, 0), PositionLine(AAPL, 2, 0))
        )
    else:
        snap_bad = snapshot(lines, book_id=book_id, msg_id=msg_id)
    assert st.on_position_snapshot(subject, snap_bad) == []
    assert st.ledger_of(BOOK) is None  # did not seed
    # nor did it burn its msg_id: a valid snapshot with the same id then bootstraps
    if msg_id == "m1":
        assert st.on_position_snapshot(BOOK, snapshot({AAPL: 3}, msg_id="m1")) == []
        assert st.ledger_of(BOOK) == {AAPL: 3}


def test_a_rejected_agreeing_snapshot_cannot_clear_a_halt() -> None:
    st, clock = bootstrapped(recon_cfg(grace_ms=0))
    snap(st, clock, {AAPL: 100}, 2)
    assert st.halt_of(BOOK) == "recon"
    clock.advance_ms(10)
    bad = snapshot({}, sent_ns=9, msg_id=LONG)
    assert st.on_position_snapshot(BOOK, bad) == []
    assert st.halt_of(BOOK) == "recon"


@pytest.mark.parametrize(
    "alert",
    [
        LimitAlert("m", 9999, 1, False, False, "t"),
        LimitAlert("m", AAPL, 9, False, False, "t"),
        LimitAlert("m", AAPL, 0, False, False, "t"),
        LimitAlert(LONG, AAPL, 1, False, False, "t"),
    ],
)
def test_rejected_limit_alerts_store_nothing(alert: LimitAlert) -> None:
    st, _ = make_state()
    st.on_limit_alert(alert)
    assert not any(k.startswith("limit.") for k in st.health(connected=True)[1])
    assert st.rejects["limit"] == 1


def test_limit_alerts_surface_in_health_and_clear_without_failing_it() -> None:
    st, _ = make_state()
    st.on_limit_alert(LimitAlert("a", AAPL, 1, False, False, "warn text"))
    st.on_limit_alert(LimitAlert("b", AAPL, 2, True, False, "hard text"))
    ok, detail = st.health(connected=True)
    assert ok  # D1's limits are D1's concern: logging and health detail only
    assert detail[f"limit.{AAPL}.vega"] == "warn" and detail[f"limit.{AAPL}.gamma"] == "hard"
    st.on_limit_alert(LimitAlert("c", AAPL, 1, False, True, "cleared"))
    assert f"limit.{AAPL}.vega" not in st.health(connected=True)[1]


def test_r14_limit_health_keys_never_exceed_universe_times_three() -> None:
    st, _ = make_state()
    for n, ins in enumerate(UNI.instruments):
        for greek in (1, 2, 3):
            st.on_limit_alert(
                LimitAlert(f"m{n}-{greek}", ins.instrument_id, greek, False, False, "")
            )
    for n in range(500):  # hostile: unknown instruments / greeks must not grow the map
        st.on_limit_alert(LimitAlert(f"h{n}", 100_000 + n, 1, False, False, ""))
        st.on_limit_alert(LimitAlert(f"g{n}", AAPL, 10 + n, False, False, ""))
    detail = st.health(connected=True)[1]
    assert sum(k.startswith("limit.") for k in detail) == len(UNI.instruments) * 3
    assert sum(k.startswith("book.") for k in detail) <= 3  # book.toml books only (1 book)


def test_health_is_not_ok_when_disconnected() -> None:
    st, _ = make_state()
    ok, detail = st.health(connected=False)
    assert not ok and detail["nats"] == "disconnected"
