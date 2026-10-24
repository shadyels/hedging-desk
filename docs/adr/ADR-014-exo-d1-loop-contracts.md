# ADR-014: EXO ↔ Delta One loop contracts (P2.M4)

**Status:** Proposed
**Date:** 2026-10-24
**Deciders:** desk lead, 2026-10-24 (D1 republishes marks; harness-published tracker flow; pseudo-random +
antithetic sampler; dirty-tree refusal; recon halt clears only on agreement or restart); acceptance of this text pending

## Context

P2.M4 is the first milestone in which an EXO number reaches the wire and in which Delta One consumes EXO
output other than targets. Several contracts the existing ADRs leave open must be fixed before code:

- how EXO learns prices;
- how EXO learns its own book's fills (netting parents report as book 0);
- how internal crosses reach EXO;
- what EXO reconciles against, and when it halts;
- the units and bump definitions of every published Greek;
- how Delta One reads EXO's Greeks for the ADR-008 Tier-1 limits;
- a unit defect in `RiskLimitAlert`;
- the meaning of the storyline's `exo_book_event` and the new `tracker_flow` action;
- whether subject tokens are numeric ids or codes.

ADR-001 (NATS core, at-most-once), ADR-004 (nothing on the hot path but in-process work; rings to gateways),
ADR-005 (netting), ADR-008 (Greek ladder) and ADR-013 (`rtrb`) bind this ADR. ADR-006 Amendment 9 records the
EXO-internal estimator decisions this ADR's units depend on.

## Decision

### 1. Subject tokens are numeric canonical ids

`<book>` is `book_id` and `<instrument>` is `instrument_id` from `protocol/refdata/universe.json` (for example,
`exo.targets.3.1001`), on every subject. Codes (`EXO-SP`) and symbols (`AAPL`) never appear in a subject.
Scenario YAML keeps codes and symbols in its fields for readability; `sim` resolves them through
`universe.json`. Comments that write `exo.targets.EXO-SP.AAPL` are prose and are corrected.

### 2. Marks: `d1.marks.<instrument>` carrying `MarkPrice`

- **Content.** Delta One republishes the mid it would use as a cross reference price, `arrival_mid_px_e9`
  (`crates/d1/src/lib.rs`), so EXO prices off exactly the number D1 crosses at. There is one implementation of
  the mid rule, and it lives in D1.
- **Path.** On every feed tick the core thread computes that mid (skipping it when ≤ 0) and pushes a `Copy`
  `MarkUpdate { instrument, mid_px_e9, feed_ts_ns }` onto a dedicated `rtrb` ring. That is the tenth core ring;
  §7 adds the eleventh. A full ring drops the update **without logging**, since the push sits on the
  tick→order path and logging there would be I/O. A core-local drop counter is printed once at shutdown.
- **Throttle and keepalive.** The gateway coalesces to the latest update per instrument. It publishes a mark
  when the mid differs from the last published mid and at least 250 ms have passed since that instrument's
  last publish (trailing edge: a suppressed change is published when its 250 ms window ends). It also
  republishes every instrument that has ever ticked once per second, as a keepalive, so a late subscriber
  converges within 1 s.
- **Prime.** The t=0 prime in `run_core` bypasses the feed arm, so primed-only instruments are never
  published as marks.
- **Timestamp — declared exception to `protocol/CLAUDE.md` rule 4.** `MarkPrice.feed_ts_ns` is Delta One's
  feed clock: ns since feed-session start, the clock `d1_core::FeedTick.exch_ts_ns` already documents (it counts
  from 0), **not** ns since the Unix epoch. It is named `feed_ts_ns`, not `exch_ts_ns`, so the difference is
  visible at every call site; it is lineage only ("this mark came from the tick at 12.000 s"). Ordering and replay
  protection use `Meta.sent_ns` (epoch): EXO accepts a mark for an instrument only if its `sent_ns` is ≥ the last
  accepted one for that instrument (non-decreasing — keepalives repeat the mid with a later `sent_ns`), and drops
  older ones silently with a counter. No plausibility band is applied to `mid_px_e9` beyond `> 0`: a magnitude
  threshold is a market convention that would have to live in refdata (root `CLAUDE.md`), and it would not stop a
  forger anyway (§13).
- `MarkPrice` carries no `book_id`. A mark is not a position, target, order, fill or cross (root invariant 2).

### 3. Delta One consumes `exo.valuations.<book>` for Tier-1 limits (ADR-008)

**Fail direction.** Every path on which Delta One cannot trust, or cannot see, a book's Greeks degrades
**toward alerting, never toward OK**. A limit that cannot evaluate its input reports at least WARN with the reason
in `text`; it never clears and never goes quiet because data stopped arriving or arrived malformed.

- **Subscription.** The D1 NATS gateway subscribes to `exo.valuations.*` and dedupes on `Meta.msg_id` (the
  existing seen-set).
- **Acceptance.** A snapshot received on `exo.valuations.<B>` is accepted only if all of the following hold:
  1. the subject token `<B>` parses as a universe `book_id` **and equals `snapshot.book_id`**;
  2. `valuation` is present with non-empty `model_id`, `params_hash` and `git_sha` (root invariant 7);
  3. `lines.len() ≤ 1024`, every line's `underlying.instrument_id` is in the universe, and every line has
     `flagged_greeks.len() ≤ 8`;
  4. `as_of_ns ≥` the last accepted `as_of_ns` for B. This is non-decreasing, not strictly increasing: EXO's
     interval republish repeats the same `as_of_ns` and content under a **fresh `msg_id`** (so it passes transport
     dedupe and refreshes receipt time), while a replayed older snapshot is rejected. `as_of_ns` is epoch UTC
     (protocol rule 4), read from EXO's wall clock when the revaluation starts, so it survives an EXO restart. A
     forged far-future `as_of_ns` locks the genuine EXO out until D1 restarts; B is then invalid and WARN-forced
     over its last accepted lines, so the lockout is loud. If the forged snapshot's lines are empty, nothing is
     floored and the per-reject log line is the only signal (within the §13 suppression ceiling).
- **Rejection is loud.**
  - Every rejection is logged with the reason, with strings truncated to 256 bytes and Debug-escaped, and counted
    per book (`u64`, printed at shutdown; surfaced on `d1.health` when that exists, P3.M1).
  - **Unattributable** (rule 1 fails): log and count only, because there is no book to hold responsible.
  - **Attributable** (rule 1 passes, a later rule fails): book B becomes **invalid**. Its last accepted lines stay
    in the aggregate, since dropping them would under-report exposure. Every underlying those lines cover is held
    at **at least WARN**, with `text = "book B: rejected snapshot (<reason>); evaluated on last accepted data"`,
    until the next accepted snapshot for B.
- **Staleness.** An accepted snapshot refreshes B's receipt time. That is a D1-local monotonic `Instant`, not
  `as_of_ns` (which does not advance while marks are unchanged) and not `sent_ns` (another host's clock). If
  nothing is accepted for B within `d1.toml [limits] valuation_max_age_s` (default 15, i.e. three of EXO's 5 s
  republish intervals), B becomes **stale**: its last lines stay in the aggregate, and its underlyings are held at
  **at least WARN** with `text = "book B: stale (no valuation for N s)"`. The next accepted snapshot clears both
  stale and invalid. A book that has never published contributes nothing and raises nothing, because Delta One
  cannot know which books EXO values (ledger row).
- **Aggregation.** Keep the latest accepted lines per book. Per underlying, sum `vega_e9`, `gamma_qty_e2` and
  `rho_e9` across books **in `i128`**. The sums are signed by design: firm exposure nets across books (ADR-008:
  firm-level limits). An offsetting *forged* line is an authentication problem (§13), not an aggregation one.
- **Levels.** Compare `sum.unsigned_abs()` (as `u128`) with the limits: HARD if `≥ hard`, WARN if `≥ warn`,
  otherwise OK. Then raise the level to at least WARN if any contributing book is invalid or stale. If the sum
  does not fit `i64` for the alert's value field, the level is HARD, `text` is "overflow", and the value is
  saturated to `i64::MAX`/`i64::MIN` by sign. There is no `abs()` on `i64` anywhere on this path.
- **Limits configuration.** Limits come from `d1.toml [limits.defaults]` and apply to every underlying.
  - At startup, all six values must be `> 0` and `warn < hard` holds per greek. The inequality is strict because
    `warn == hard` would make WARN unreachable.
  - `[limits] valuation_max_age_s` must be `> 0`.
  - A `[limits]` table containing anything other than `defaults` and `valuation_max_age_s` is a startup error.
- **No D1-own contribution.** Delta One's own book contributes no vega, gamma or rho, since it holds linear
  instruments only in P2. D1-held listed options carry vega it cannot price; that is a P4.M3 concern and is
  recorded as a ledger row. ADR-008's "aggregates its own delta" feeds `RiskSnapshot`, which is deferred to P3.M1.
  No delta limit exists in `d1.toml`.
- **Alert on change only.** A `RiskLimitAlert` is published only when the level changes:
  - into WARN: `breached=false, cleared=false`, which includes a WARN forced by invalid or stale data, with the
    reason in `text` and the last aggregated value in the value fields;
  - into HARD: `breached=true`;
  - back to OK: `cleared=true`.

  There is no hysteresis (ledger row). The check runs only in the gateway thread, never on the core thread or the
  hot path.

### 4. `RiskLimitAlert` unit fix (additive)

Gamma's unit is `qty_e2` ("dDelta per +1% spot move"; `d1.toml` `gamma_*_qty_e2`). Vega and rho are
ccy × 1e9. The message gains:
- `value_qty_e2` and `limit_qty_e2`, populated for GAMMA only. `value_e9`/`limit_e9` are 0 for gamma and are
  never used for it.
- `text`: human-readable, with the unit, for example "AAPL vega 62,000.00 USD/vol-pt >= warn 50,000.00".
- `cleared`.

The field-4 comment's "qty_e2 semantics noted in text" is replaced. There is still no `book_id`: Tier-1
limits are firm-level per underlying (ADR-008).

`Meta.schema_version` is per message type: 1 for a new message, incremented by one for each additive change to
that message's schema, including nested messages. In P2.M4, `ValuationSnapshot` (via `ProductLine` fields 12–20)
and `RiskLimitAlert` (fields 7–10) publish 2, and every other message publishes 1. Consumers never reject a
message on its version.

### 5. Per-book fills: allocation `ExecutionReport`s on `d1.exec.<book>.<instrument>`

- **Report types.** For a netting parent order, Delta One keeps publishing the firm-level report on
  `d1.exec.0.<instrument>`, unchanged. For every allocation that `allocate_fill` produces and
  `PositionKeeper::apply_fill` **successfully books**, it additionally publishes one report on
  `d1.exec.<book>.<instrument>` with these fields:
  - `book_id` = the allocated book;
  - `cl_ord_id` and `exec_id` = the parent's and the venue exec's;
  - `side` = the parent's;
  - `last_qty_e2` = this book's allocated quantity;
  - `last_px_e9` = the fill price;
  - `status`, `cum_qty_e2` and `leaves_qty_e2` = **the parent's** (ledger row: no per-book cumulative and no
    allocation flag until P3.M1's blotter needs one).

  An allocation that fails to book publishes nothing, so EXO never counts a fill D1 did not book. Direct
  (non-parent) orders report exactly as today on their own book.
- **Rule for consumers.** A report with `book_id ≠ 0` and `last_qty_e2 > 0` is a fill **to that book**.
  Consumers that aggregate fills dedupe on the business key **(`exec_id`, `book_id`)** as well as transport
  `msg_id`. Applying a fill is not idempotent, so `msg_id` alone cannot catch a logical re-emission under a
  fresh envelope.
- **Ordering.** Book-0 parent reports never appear on a book-N subject, and EXO subscribes only to its own
  books (`d1.exec.3.>`, `d1.exec.4.>`). Allocation reports are pushed before the parent's book-0 report on the
  same ring, so within a gateway iteration they publish first.

### 6. EXO consumes `d1.crosses`

Internal crosses move EXO books with no exec report. EXO subscribes to `d1.crosses` and, for each
`InternalCrossNotice` touching one of its books, applies `+qty_e2` to `buy_book_id` and `−qty_e2` to
`sell_book_id` on `instrument`. It dedupes on the business key `cross_id` as well as `msg_id`. This covers
netting crosses and directed transfers alike, since they share one booking path (ADR-009).

### 7. `PositionSnapshot` cadence and content (`d1.positions.<book>`)

- **Mirror.** After any core-loop arm that can change a position or inflight on instrument I (target cycle,
  booked transfer, applied exec), the core thread pushes one `Copy` `PositionUpdate { book, instrument, qty_e2,
  avg_px_e9, inflight_qty_e2 }` per configured book for I onto a dedicated ring. That is the eleventh ring,
  with capacity 1024, log-and-drop. Values are absolute, so a later update heals a dropped one.
- **Publish.** The gateway keeps a mirror. It publishes:
  - a snapshot for each book whose mirror changed, coalesced once per gateway loop iteration;
  - every configured book every 1 s, flat books included, with an empty `lines`.

  Lines list instruments with non-zero `qty_e2` or `inflight_qty_e2`; a missing line means 0.
- **Drain order.** Within an iteration the gateway drains and publishes in this order: exec reports, crosses,
  tracker, marks, position updates and snapshots. That minimises snapshot-ahead-of-report transients; §8's
  grace period absorbs the rest.

### 8. Reconciliation (EXO side; `exo/CLAUDE.md` rule 5)

- **Ledger.** EXO keeps `ledger[(book, instrument)] = qty_e2` (an integer; no float anywhere) for its own
  books. It is fed only by §5 fills and §6 crosses.
- **Bootstrap.** At startup a book's ledger is unset. The first `PositionSnapshot` for that book seeds it,
  and EXO publishes **nothing** on `exo.targets.<book>.*` before that. The adopted values are logged.
  Bootstrap assumes quiescence: a fill or cross in flight across the bootstrap snapshot double-counts and
  leads to a halt, which is the safe direction (ledger row).
- **Accepting a snapshot.** EXO accepts a `PositionSnapshot` received on `d1.positions.<B>` only if all of the
  following hold:
  - the subject token equals `book_id`;
  - `lines.len() ≤` the universe instrument count;
  - `Meta.sent_ns ≥` the last accepted one for B (non-decreasing; the replay guard).

  A rejected snapshot is logged and counted. It neither seeds, nor compares, nor clears, so a replayed older
  agreeing snapshot cannot clear a halt.
- **Arithmetic.** Ledger updates are checked against the sint64 range. A result outside it is treated as a
  divergence on that instrument.
- **No-snapshot halt.** If a bootstrapped book has no accepted snapshot for `[recon] max_snapshot_gap_ms`
  (default 5000, five of D1's 1 s periodic snapshots), EXO halts that book's targets with reason `no-snapshot`.
  This is health-only, with no `ReconAlert`: there are no views to report. The halt clears on the next accepted
  snapshot that agrees, also health-only (no `ReconAlert` on clear either); as with a divergence un-halt, the
  latest computed target is then published whatever the band. Without this, a D1 clock step-back (every
  snapshot rejected by the guard) or a lost subscription would leave recon blind while targets kept flowing.
- **Transport dedupe.** EXO's `msg_id` dedupe is a bounded FIFO of the last 65,536 ids. Evicting an id is safe:
  every state-changing message also carries a business key (`(exec_id, book_id)`, `cross_id`) or a `sent_ns`
  guard. The business-key sets are unbounded and grow only with booked activity (ledger row).
- **Compare.** On every snapshot, for each instrument in the union of ledger keys and snapshot lines,
  compare `|ledger − snapshot.qty_e2|` against `[recon] tolerance_qty_e2` (default `0`, because both sides are
  exact integers). Inflight is not compared.
- **Halt.** If a book is divergent on every snapshot accepted over at least `[recon] grace_ms` (default
  1500, which spans at least one 1 s periodic snapshot after the first divergent one), EXO:
  - publishes one `ReconAlert{book_id, instrument, exo_view_qty_e2, d1_view_qty_e2, publishing_halted=true}`
    on `exo.alerts.recon`, for the first divergent instrument;
  - stops publishing targets for that book;
  - continues to publish `ValuationSnapshot`s, because monitoring survives the halt;
  - reports the halt in `exo.health`.
- **Un-halt.** On the first later snapshot on which every instrument of the book is within tolerance, EXO
  publishes one `ReconAlert{…, publishing_halted=false}` (views equal) and resumes. Its latest computed target
  is published immediately, whatever the band, because the halt may have suppressed a real rehedge.
  Otherwise the halt persists until an EXO restart re-bootstraps. Operator re-seed is P3.M2.
- **Wire semantics.** `ReconAlert` is published on transitions only. `publishing_halted=true` means halt
  engaged; `publishing_halted=false` means halt cleared.
- **Delta One.** D1 takes no action on `exo.alerts.recon` in P2 and is removed from that row's consumers.
  EXO's halt is the control.

### 9. Greek units on the wire (all desk-position values)

Every `ValuationSnapshot.ProductLine` value is the **book's position** value: `desk_quantity ×` the per-unit
product value, where `desk_quantity` comes from the persisted book and is negative when the desk issued the
product. Lines therefore sum to book totals, and `book_pv_e9 = Σ pv_e9`. Amounts are in the product currency, which for
every product the P2.M4 book can load equals the underlying's `currency` in refdata. There are no quanto products
(ADR-006 Amendment 9 §7's loadable list), so Delta One's per-underlying sums never mix currencies. A quanto product
would need a currency field and per-currency limits (P4.M5).

| Field | Definition (bump method: ADR-006 Amendment 9 §2) | Unit |
|---|---|---|
| `pv_e9` / `pv_std_err_e9` | MC price and its pair-mean SE | ccy × 1e9 |
| `delta_qty_e2` | ∂PV/∂S | units of underlying × 1e2 (EUR for EURUSD) |
| `gamma_qty_e2` | change in delta for +1% spot = Γ · 0.01 · S, with S = the mark the Greeks were computed at | units × 1e2 per +1% of the mark |
| `vega_e9` | ∂PV/∂σ × 0.01, with σ = √v0 and √θ shifted together | ccy × 1e9 per +1 vol point |
| `rho_e9` | ∂PV/∂r × 1e-4 (domestic, quote-ccy rate; moves drift and discount) | ccy × 1e9 per +1bp |
| `div_sens_e9` | ∂PV/∂q × 0.01, for EQUITY/INDEX/ETF underlyings; 0 for FX | ccy × 1e9 per +1% yield |
| `rho_foreign_e9` (new) | ∂PV/∂q × 1e-4, for FX_SPOT underlyings (q := r_f, Amendment 8 §1); 0 otherwise | ccy × 1e9 per +1bp foreign rate |
| `theta_e9` | (PV aged one model step − PV) / (dt · `[greeks] theta_days_per_year`), with S and v held | ccy × 1e9 per calendar day |
| `*_std_err_*` (new) | the pair-mean SE of each estimate, same unit | as the estimate |
| `flagged_greeks` (new) | names of Greeks with `se > max_rel_std_error · abs(value)`; a 0 value is always flagged; only Greeks applicable to the line are labelled (`div_sens` or `rho_foreign`, not both) | — |

- **Hedge target.** For each (book, underlying), the target is `target_qty_e2 = −Σ_lines delta`, rounded
  once, half-even, to a multiple of the instrument's `lot_e2` from refdata.
- **Band.** `band_qty_e2` comes from `exo.toml [rehedge]` by instrument class (EQUITY, INDEX or ETF → `band_equity_qty_e2`;
  FX_SPOT → `band_fx_qty_e2`; any other class is a load error).
- **Republish rule.** EXO publishes a target when either:
  - none has been published for the pair yet, or
  - `|new − last_published| > band_qty_e2`, or
  - an un-halt occurred (§8).
- `reason` states the trigger and lists flagged Greeks, for example
  "rehedge: |Δtarget|=457,900 > band 50,000; mark 176.000000000; flagged: delta,gamma,vega,rho,theta,div_sens".
- **Provenance.** `ValuationMeta` on the snapshot and on every target of the same book and cycle is the
  book's per-cycle run manifest (Amendment 9 §5). `model_id` is `heston-<scheme>-pseudo-antithetic-v1`, which
  closes Amendment 6's "sampler indistinguishable on the wire" gap for P2.M4. `git_sha` carries `-dirty` only
  under the explicit opt-in (Amendment 9 §6).

### 10. Storyline actions: `exo_book_event` and `tracker_flow`

- **`action: exo_book_event`** (`book`, `note`) is a **harness checkpoint**, not a market event. `sim` emits
  no tick for it. The EXO book is loaded at EXO startup. The storyline harness asserts three things by the
  entry's `at_ms`, a deadline in ms from D1 start:
  - EXO has published a `ValuationSnapshot` on `exo.valuations.<book_id>` with at least one line and full
    `ValuationMeta`;
  - EXO has published a `TargetPosition` for each of that book's underlyings;
  - D1's `d1.positions.<book_id>` shows each such target fully filled.

  This makes "re-published" in the next expect meaningful.
- **`action: tracker_flow`** (`book`, `instrument`, `target_qty`, `band_qty`, `note`; quantities in
  decimal units) makes the harness publish a `TargetPosition` for `book` (expected: D1-CORE) on
  `exo.targets.<book_id>.<instrument_id>`. The message carries:
  - `producer="sim"`, a fresh UUIDv7 `msg_id`, and no `valuation`;
  - `target_qty_e2` and `band_qty_e2` converted from the decimals;
  - `reason="tracker_flow: <note>"`.

  It stands in for an index-replication source (marker and ledger row;
  desk lead, 2026-10-24), exactly as
  `crosses_round_trip.rs` already does. A `band_qty` ≥ |demand| keeps that demand live for netting
  (ADR-005 §2 as amended below), which is how the storyline gets a cross.
- **`expect:` entries** are satisfied in YAML order. Expect k's event must be received after:
  - the wall-clock time of the last non-`quote` *action* that precedes it in the YAML (heartbeat quotes never
    trigger);
  - the event that satisfied expect k−1.

  It must be received before its own `at_ms` deadline, measured from D1 start. The known kinds are the nine
  in the two repository scenarios; an unknown kind is a parse error. The harness implements four;
  the others raise "not implemented before P4" if run.

### 11. Band semantics (amends ADR-005 §2)

`TargetPosition.band_qty_e2` is **authoritative**: each producer declares its book's no-trade band with its
target. ADR-005 §2 is amended to read as in that ADR's Amendment (2026-10-24). EXO's own republish band (§9)
and D1's external-order band are the same number. An EXO book's unhedged drift is therefore bounded by its
band, not by twice its band: a republish creates demand larger than that band, and D1 applies a band no wider
than it (the minimum across books).

### 12. `exo.health`

1 s heartbeat. `ok` is false when EXO is NATS-disconnected, refusing to publish (dirty tree), has any book
halted, or has any book whose latest valuation attempt failed (§15). `detail` keys:
- `git_sha`;
- `publish` (`enabled` or `refused: dirty tree …`);
- `book.<id>.state` (`awaiting_snapshot` | `awaiting_marks` | `publishing` | `halted:recon` | `halted:no-snapshot`);
- `book.<id>.last_valuation_ns`;
- `valuation_in_flight`;
- `limit.<instrument_id>.<greek>`, holding the latest non-cleared `RiskLimitAlert` level. EXO consumes
  `d1.alerts.limits` for logging and health only.

### 13. Transport trust (ceiling accepted for the single-host demo)

NATS runs with no authentication, no authorization and no TLS (`deploy/nats.conf`), and compose publishes
4222/8222/9222 on all interfaces. Any client that reaches the server can publish on any subject. That includes
`exo.targets.>`, which Delta One has executed from any publisher since P1.M2, and this ADR's safety inputs
(`d1.marks.*`, `d1.positions.*`, `d1.exec.*`, `d1.crosses`, `exo.valuations.*`). P2.M4 does not change the worst
case — a forged target already trades — and accepts it for the demo. Consequently no consumer treats
`Meta.producer` or any payload field as proof of origin; the guards in §2, §3 and §8 defend against redelivery,
replay and malformed data, **not** against forgery. Forging `exo.valuations.*` adds one capability a forged
target does not: suppressing Tier-1 limit alerts with zeroed or offsetting lines (§3). It is monitor-only, not a
trade path, and sits inside this accepted ceiling. Upgrade (marker in `deploy/nats.conf`; ledger row): per-
component `authorization` users with publish/subscribe permissions, loopback-bound client/monitoring/WebSocket
ports and TLS on 9222 — owner P3.M2, whose `d1.cmd.order` turns the anonymous WebSocket into an order-entry path,
or earlier for any non-single-host deployment.

### 14. Bounds on untrusted fields

Consumers treat every string and repeated field as untrusted display data.

- **Strings.** Truncate to 256 bytes before logging or inserting into health. Log them escaped (Rust `{:?}`,
  Python `repr`). The UI renders them as text, never as HTML (P3.M1).
- **Repeated fields.**
  - Delta One rejects a `ValuationSnapshot` with more than 1,024 lines, or a line with more than 8
    `flagged_greeks`.
  - EXO rejects a `PositionSnapshot` with more lines than the universe has instruments.
- **Producer side.** EXO caps its own `reason`/`text` output at 256 bytes.
- **Health detail.** `exo.health.detail` carries `book.*` keys only for books in `book.toml`, and `limit.*` keys
  only for universe instruments × the three limited Greeks. Alerts for other instruments are logged and not
  stored, so the map is bounded by configuration, not by message content.
- **Payload size.** `deploy/nats.conf` sets `max_payload: 1MB`. That is the server default, now stated explicitly.

### 15. Revaluation pacing and failure (EXO)

A book is revalued at most once per `[valuation] min_revaluation_gap_s` (default 1 s), measured from the end of
its previous job on the monotonic clock, and later marks coalesce. A job running longer than `job_timeout_s`
(default 30 s) is fatal: EXO reports `ok=false` once and exits, because a hung worker cannot be reclaimed safely,
and the restart re-bootstraps under §8. A job that fails publishes nothing and is retried on the next changed
mark or after `retry_after_s` (default 10 s), whichever is first. While a book's latest attempt has failed, its
snapshot is **not** republished on the interval timer and `exo.health ok` is false. Delta One's §3 staleness
therefore raises WARN instead of a stale number masquerading as current. Replay guards reject `sent_ns` more
than `[bus] max_clock_skew_s` (default 60 s) ahead of local wall time. A band bypass owed by an un-halt (§8) is
kept until a target publication actually goes out.

## Consequences

- **Easier.** The loop closes with no new dependency in either language. D1 limit checking stays off the core
  thread. Every EXO number on the wire is reproducible from its manifest, and fills are idempotent by business
  key.
- **Harder.** D1 owns two more rings (11 total) and a position mirror. EXO owns a ledger that halts on any
  lost message (NATS core is at-most-once). The storyline needs a running EXO, `uv`, NATS and the sim
  acceptor, so it is an `#[ignore]` test that CI never runs.
- **Not done here:**
  - `RiskSnapshot` (P3.M1);
  - per-book cumulative quantity and an allocation flag on reports (P3.M1);
  - operator re-seed (P3.M2);
  - foreign-rho limits and rho bucketing (P4.M1);
  - limit hysteresis;
  - limits for books that have never published (Delta One cannot know which books EXO values);
  - NATS authentication and authorization (§13; P3.M2).
