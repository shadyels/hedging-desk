# Ponytail Debt Ledger

**28 markers found, 5 with no upgrade trigger.**

## By File

| File | Line | Simplified | Ceiling | Upgrade |
|------|------|-----------|---------|---------|
| `delta-one/crates/d1-core/src/keeper.rs` | 117 | Weighted-average cost-basis (no lot tracking, no realized P&L) — **per-book cash half DELIVERED P1.M5 Slice 1** | cash is now tracked per book (`seed_cash` at startup, moved on every fill, `credit_dividend` on a dividend tick, `accrue_cash` per sample); lot tracking, realized P&L and the Σ-book-position invariants remain | partially closed — cost-basis simplification still stands |
| `delta-one/crates/d1-core/src/order.rs` | 28 | FIX OrderCancelRequest not driven | Slice 1 only | FIX gateway P1.M2 Slice 2 |
| `delta-one/crates/d1-core/src/order.rs` | 32 | FIX OrderCancelReplaceRequest not driven | Slice 1 only | FIX gateway P1.M2 Slice 2 |
| `delta-one/crates/d1-core/src/order.rs` | 147 | Append-only slab, no freelist | single-session memory | freelist + eviction policy for long uptime (P1.M2 Slices 2-3) |
| `delta-one/crates/d1-core/src/order.rs` | 221 | Exec validation debug-only | compile-time checks | promote to error if live venue sends malformed execs |
| `delta-one/crates/d1-core/src/cross.rs` | 41 | ⚠️ **no-trigger** — omits `transfer_id`/`reason` | intentional M4 omission | none named |
| `delta-one/crates/d1-core/src/ids.rs` | 48 | FIX ExecID capped at 20 bytes | truncation on oversize | Slice 2: reject/hash oversize, never truncate |
| `delta-one/crates/d1-gateway-nats/src/lib.rs` | 60 | ⚠️ **no-trigger** — NATS startup failure degrades | intentional DoD behavior | none named |
| `delta-one/crates/d1-gateway-nats/src/lib.rs` | 173 | `seen_msg_ids` unbounded dedup cache | unbounded memory | eviction policy or JetStream dedupe for long uptime |
| `delta-one/crates/d1-gateway-nats/src/lib.rs` | 213 | Log-and-drop on full ring | demo-sized ring | backpressure protocol |
| `delta-one/crates/d1/src/main.rs` | 120 | ⚠️ **no-trigger** — NATS errors logged, not fatal | intentional degradation | none named |
| `delta-one/crates/d1-gateway-fix/src/convert.rs` | 34 | No `TransactTime` (60) field | missing required spec field | add time dependency + field if real venue requires validation |
| `delta-one/crates/d1-gateway-fix/src/convert.rs` | 51 | `limit_px_e9 == 0` as market-order sentinel | ambiguous encoding | explicit `OrdType` enum field when beyond market-only |
| `delta-one/crates/d1-gateway-fix/src/convert.rs` | 214 | Negative fixed-point input unguarded | malformed FIX output possible | `debug_assert!(value_e_n >= 0)` |
| `delta-one/crates/d1-gateway-fix/src/convert.rs` | 228 | Lenient precision handling | tolerance for mismatch | tighten if real venue needs exact preservation |
| `delta-one/crates/d1/src/lib.rs` | 323 | Log-and-drop on full ring | demo-sized ring | backpressure protocol |
| `delta-one/crates/d1/src/lib.rs` | 352 | ⚠️ **no-trigger** — dropped push orphans registration | inflight weight permanent | none named |
| `delta-one/crates/d1/src/lib.rs` | 408 | No numeric upper bound on `qty_e2` — **NOT delivered by P1.M5** | still open: a business max-transfer-size limit is a Tier-1 risk check, not a code constant. Overflow itself stays guarded by `apply_cross`'s `checked_*` arithmetic rejecting the cross outright | Tier-1 risk check in `d1.toml` (ADR-008), P4 |
| `delta-one/crates/d1/src/lib.rs` | 606 | Log-and-drop on full ring (ExecReport) | demo-sized ring | backpressure protocol |
| `delta-one/crates/d1-posttrade/src/producer.rs` | 56 | Plaintext Kafka broker connection (no SASL/TLS) | matches compose `PLAINTEXT`-only listener; local-demo only | `security.protocol=SASL_SSL` + broker listener before any non-localhost broker |
| `delta-one/crates/d1-posttrade/src/producer.rs` | 83 | Log-and-drop on local-queue-full send | demo-sized producer queue | retry/backpressure protocol |
| `delta-one/crates/d1/src/lib.rs` | 771 | `arrival_mid_px_e9` returns `0` for a never-ticked instrument; `book_cross` books at that price with no guard | unreachable today (feed covers whole keeper universe + t=0 priming is unconditional); a cash-aware keeper makes the consequence worse — a real position with zero cash paid means free NAV and therefore garbage TE | reject cross when `ref_px_e9 == 0` / when the quote has never ticked |
| `delta-one/crates/d1-netting/src/lib.rs` | 23 | One `CrossRefPrice` variant only | arrival-mid default only | add `ExecVwap` when ADR-005 §29 resolved |
| `delta-one/crates/d1-netting/src/lib.rs` | 196 | Band computed as `min()` over books | suboptimal netting band | instrument-level band in `protocol/refdata/universe.json` (ADR-005 §2 / ROADMAP.md:12) |
| `delta-one/crates/sim/src/acceptor.rs` | 221 | ⚠️ **no-trigger** — static counter for demo | single-process only | none named ("no ADR-worthy") |
| `delta-one/crates/d1-gateway-fix/initiator.cfg` | 5 | `UseDataDictionary=N` (no spec validation) | no FIX schema enforcement | enable if real venue requires validation |
| `delta-one/crates/d1-gateway-fix/src/lib.rs` | 84 | Demo-sized ring, silent drop on full | demo-capacity only | size for production load |
| `delta-one/crates/d1-gateway-fix/src/lib.rs` | 146 | Log-and-drop, no retry/dead-letter | ephemeral loss | retry + dead-letter for production |

## Patterns & Risk Clusters

### Backpressure (6 markers)
Six sites use log-and-drop on a full ring/queue, all gated on implementing a "backpressure protocol":
- `d1-gateway-nats/lib.rs:213`
- `d1/src/lib.rs:323`, `606`
- `d1-posttrade/src/producer.rs:83`
- `d1-gateway-fix/src/lib.rs:146`

**Risk:** Silent data loss under load. Upgrade together when load testing shows the need.

### Unbounded Growth (3 markers)
Three dedup/cache structures grow without bound, all gated on "long uptime":
- `d1-core/src/order.rs:147` — `seen_execs` slab slots
- `d1-gateway-nats/lib.rs:173` — `seen_msg_ids` dedupe map
- `d1-gateway-fix/src/lib.rs:84` — exec event ring

**Risk:** OOM on continuous operation beyond single session. Upgrade together during P1.M2 Slices 2-3 when real gateways drive long-running sessions.

### Post-trade Kafka demo ceilings (P1.M4 Slice 2, 1 marker)
The Kafka wiring ships with one demo-grade shortcut:
- `d1-posttrade/src/producer.rs:56` — plaintext connection (SASL_SSL before non-localhost)
- (`producer.rs:83` counted under Backpressure)

**Risk:** not a correctness bug at demo scope; becomes real before a non-local broker.

### No Upgrade Path (5 markers) ⚠️
- `d1-core/src/cross.rs:41` — deliberately omitted for P1.M4 (Kafka lineage); verified still not in `posttrade_cross.avsc` at Slice 2
- `d1-gateway-nats/src/lib.rs:60` — intentional degradation (DoD #4)
- `d1/src/main.rs:120` — intentional degradation (DoD #4)
- `sim/src/acceptor.rs:221` — explicitly marked "no ADR-worthy"
- `d1/src/lib.rs:352` — orphaned registration, no named path

These are deliberate design choices or deferred decisions, not tech debt to upgrade. Flag if behavior expectations change.

### Phase-Gated Upgrades
- **P1.M2 Slice 2 FIX wiring:** `order.rs:28,32` (cancel/replace), `ids.rs:48` (ExecID validation), `convert.rs:34` (TransactTime)
- **P1.M2 Slices 2-3 long-uptime:** `order.rs:147` (freelist), `d1-gateway-nats/lib.rs:173` (eviction)
- **P1.M3 netting:** `keeper.rs:117` (cost-basis full invariants)
- **P1.M5 tracker analytics:** `keeper.rs:117` per-book cash — **delivered** (Slice 1). `lib.rs:408` qty upper bound — **not** delivered; it was mis-scoped here, being a Tier-1 risk check (ADR-008), not a cash concern. Re-targeted to P4.
- **ADR-005 compliance:** `d1-netting/lib.rs:23` (ExecVwap), `lib.rs:196` (instrument band), `convert.rs:51` (OrdType enum)

**P1.M5 markers (tracker analytics):** benchmark composition is a PRICE return while book NAV is a TOTAL return — constituent dividends land entirely in tracking difference, appearing as an ~8bps active-return spike on the ex-dividend sample, which in a short window dominates the variance; end-of-session Kafka cadence is a process-boundary limitation (a real deployment triggers on EOD, this process never sees it); `publish_interval_s` is parsed and range-validated but not wired (publishes on every sample, not separately cadenced); second corporate-action transport type needed (currently only `FeedTick.div_per_share_e9`); integration-coverage gap: `DIVIDEND_AT_NS` (9s) never fires during golden run, cannot be closed without rewriting all four golden fixtures.

**P1 exit-criteria markers (tick-to-trade bench, `feat/d1-t2t-bench`):** four shortcuts sit behind the Phase 1 exit criterion now recorded as met in `docs/ROADMAP.md`.

- **Unpinned cores — ADR-004 §5 half-unmet.** §5 asks for HDR histograms "on release builds with **pinned cores**". The release-build half holds (`bench` profile inherits `release`); the pinning half does not. macOS/Apple Silicon does not honour thread-to-core affinity — the Mach `thread_policy_set` affinity tag is a hint Apple Silicon ignores — so a `core_affinity` call would compile, run, and silently do nothing, which is worse than not pinning, because the report would claim a property it does not have. No dependency was added; `benches/tick_to_trade.rs` prints an explicit UNPINNED note instead. *Upgrade:* a Linux CI runner, where affinity is real, is the only path that closes this.
- **Composed-path, not live-loop causal.** The reported T2T number assembles the four ADR-004 §1 stages in one timed closure in `run_core`'s live call order; it is not a timestamp taken across a real tick flowing through the running process. The sequence is target-triggered, which ADR-004 §1's own parenthetical ("the next tick *or an immediate synthetic evaluation event*") covers, so this is a measurement-fidelity gap, not an ADR violation. Read this precisely: it does **not** mean a causal path exists and merely went uninstrumented. There is no tick-triggered evaluation arm in `run_core` at all — feed ingest and target-driven netting sit in two separate `if let Ok(…)` arms of the same poll loop — so the bench composes a sequence the process never today executes end to end from a single stimulus. *Upgrade:* a live measurement needs only a tick-triggered evaluation arm in `run_core` plus ingress/egress timestamps — notably **not** new state, because `NettingSession::targets` (`crates/d1/src/cycle.rs:117`) already *is* the persistent desired-position table ADR-004 §1 describes.
- **The measured path allocates and does I/O.** `NettingSession::on_target` performs ~6 heap allocations per cycle (`crates/d1/src/cycle.rs:197`, `214`, `230`, `250`, `259`, `307`, `316`) plus an unconditional `println!` on every parent-order mint (`:320`) — all inside the window ADR-004 §1 budgets. This sits in tension with `delta-one/CLAUDE.md`'s hot-path contract (rule #1 no heap allocation, rule #3 no syscalls/I/O/logging "on any code reachable from the hot path") while `cycle.rs:6-9`'s own module doc declares the module off the benchmarked hot path. Both cannot hold: `docs/ARCHITECTURE.md` budgets "Netting evaluation (incremental) ≤ 10 µs" as a T2T stage and `run_core` reaches it through `on_target`. The four isolated stage benches could never expose this, because they only ever covered the alloc-free crates (`d1-core`, `d1-netting`) — the allocating orchestration layer *between* them was on no measured path. Latency consequence today is bounded (measured p50 is roughly an order of magnitude under budget) but it is headroom being spent, and the `println!` alone drives the run-to-run p99 variance. There is also a second-order **operational** consequence: that `println!` fires once per netting cycle and embeds `ClOrdId`'s derived `Debug` over `[u8; 20]` (`crates/d1-core/src/ids.rs:15-16`), rendering ~200 bytes as `ClOrdId([48, 48, 48, …])`. Across a bench run's ~10⁶ cycles that is **~245 MB of stdout per `just bench` run, measured** — which buries the ADR-004 §5 report lines it is supposed to deliver and would make any CI job capturing that output produce a quarter-gigabyte artefact. Until the `println!` moves to a telemetry ring, run `just bench` with stdout redirected. *Upgrade:* **owned by P4.M2** (rho transfer — the first milestone that adds work through `cycle.rs`'s cross-booking path, since `on_transfer` books a directed cross through the same `book_cross` helper netting-derived crosses use; P4.M3's proposal execution then rides the same path). Decide which document is wrong: either make `cycle.rs` alloc-free and route its logging through a telemetry ring (rule #3's prescribed shape), or amend `delta-one/CLAUDE.md`'s hot-path contract to state that netting *orchestration* is explicitly outside it and adjust `docs/ARCHITECTURE.md`'s per-stage row accordingly. The same decision covers `run_core`'s ingest-arm `println!` (`crates/d1/src/lib.rs:448-453`), which is the same class of demo-grade stdout logging and sits just outside the benched window.
- **`d1` cannot be fed from a scenario file.** `crates/d1/src/feed.rs` is a synthetic random walk; there is no transport from a scenario timeline into the running process. `crates/sim` is bin-only (`main.rs`, no `lib.rs`), so its `replay.rs` cannot be depended on as a library, and `sim.md.<instrument>` is UI-display-only by contract (`protocol/nats-subjects.md:23`: "Delta One ingests via its feed boundary, not via NATS"). This is the remaining prerequisite for **P2.M4**'s "Full tracker-flow demo storyline runs", and is why that conjunct was removed from the P1 exit criterion rather than implemented. *Upgrade:* P2.M4 — give `sim` a lib target or a scenario-driven feed transport into `d1`'s feed boundary.

---

*Generated by `ponytail-debt` skill on 2026-07-21; P1 exit-criteria markers appended 2026-08-04.*
