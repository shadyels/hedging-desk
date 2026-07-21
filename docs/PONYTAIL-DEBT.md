# Ponytail Debt Ledger

**30 markers found, 5 with no upgrade trigger.**

## By File

| File | Line | Simplified | Ceiling | Upgrade |
|------|------|-----------|---------|---------|
| `delta-one/crates/d1-core/src/keeper.rs` | 117 | Weighted-average cost-basis | no lot tracking, no realized P&L | P1.M3 netting / P1.M5 tracker analytics |
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
| `delta-one/crates/d1/src/lib.rs` | 408 | No numeric upper bound on `qty_e2` | no compile-time constraint | Tier-1 risk check in `d1.toml` (ADR-008) |
| `delta-one/crates/d1/src/lib.rs` | 472 | Only fill transitions audited; non-fill terminal execs (reject/cancel/expire) not published to `posttrade.orders.audit` | audit trail stuck at `New` on rejects | wire the `fill == None` arm (Slice 3+); `order_audit` already supports it |
| `delta-one/crates/d1/src/lib.rs` | 606 | Log-and-drop on full ring (ExecReport) | demo-sized ring | backpressure protocol |
| `delta-one/crates/d1/src/posttrade.rs` | 9 | Hardcoded `"INTERNAL"`/`"SIM"` counterparty strings | no venue/broker registry | promote when a second real venue exists |
| `delta-one/crates/d1-posttrade/src/producer.rs` | 56 | Plaintext Kafka broker connection (no SASL/TLS) | matches compose `PLAINTEXT`-only listener; local-demo only | `security.protocol=SASL_SSL` + broker listener before any non-localhost broker |
| `delta-one/crates/d1-posttrade/src/producer.rs` | 83 | Log-and-drop on local-queue-full send | demo-sized producer queue | retry/backpressure protocol |
| `delta-one/crates/d1-posttrade/src/producer.rs` | 104 | In-process topic-ensure on every startup | not deploy-time provisioning | promote to `scripts/demo.sh` / deploy-time provisioning when topic config needs managing outside code |
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

### Post-trade Kafka demo ceilings (P1.M4 Slice 2, 5 markers)
The Kafka wiring ships with demo-grade shortcuts, each with a named upgrade path:
- `d1-posttrade/src/producer.rs:56` — plaintext connection (SASL_SSL before non-localhost)
- `d1-posttrade/src/producer.rs:104` — in-process topic-ensure (deploy-time provisioning later)
- `d1/src/posttrade.rs:9` — hardcoded `INTERNAL`/`SIM` counterparties (venue registry later)
- `d1/src/lib.rs:472` — non-fill terminal audits unwired (Slice 3+)
- (`producer.rs:83` counted under Backpressure)

**Risk:** none is a correctness bug at demo scope; all become real before a non-local broker or a compliance-complete audit stream.

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
- **P1.M4 Slice 3 post-trade e2e:** `d1/src/lib.rs:472` (non-fill audits), `d1/src/posttrade.rs:9` (counterparties), `producer.rs:104` (topic provisioning)
- **P1.M5 tracker analytics:** `keeper.rs:117`, `lib.rs:408` (per-book cash, qty upper bound)
- **ADR-005 compliance:** `d1-netting/lib.rs:23` (ExecVwap), `lib.rs:196` (instrument band), `convert.rs:51` (OrdType enum)

**Next milestone:** Review before P1.M4 Slice 3 start; the post-trade Kafka demo ceilings cluster there.

---

*Generated by `ponytail-debt` skill on 2026-07-21.*
