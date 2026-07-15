# CLAUDE.md — delta-one (Rust)

Delta One engine: linear hedging, firm-wide netting, execution, and the three outbound planes (NATS, FIX 4.4, Kafka). Latency-sensitive. Read the root `CLAUDE.md` first; invariants there apply here.

## Crate layout (Cargo workspace)

| Crate            | Role | Hot path? |
|------------------|------|-----------|
| `d1`             | process binary: owns the core thread (`OrderStore` + `PositionKeeper`) and starts the gateways, wired together over `rtrb` rings (ADR-013) | no (hosts the hot path) |
| `d1-core`        | market-data ingest, position keeper (incl. per-book cash, ADR-010), order state machine, risk checks | **yes** |
| `d1-netting`     | firm-wide netting across books, internal-cross generation (ADR-005) | **yes** |
| `d1-gateway-nats`| NATS in/out: consume EXO targets + UI commands, publish exec reports & risk | no |
| `d1-gateway-fix` | FIX 4.4 session + message build/parse via `quickfix` crate (ADR-003) | edge |
| `d1-posttrade`   | Kafka producer, Avro encoding, booking events (ADR-002) | no |
| `d1-analytics`   | tracker analytics: ex-post TE, tracking difference, cash drag (ADR-010) | no |

Threading model: one pinned thread per hot-path stage, communicating over bounded SPSC ring buffers, `rtrb` for every ring (ADR-013 — bounded `crossbeam-channel` rejected, its blocking ops lock and it's MPMC machinery this project never needs). Gateways run on separate threads/tokio runtimes and exchange data with the core only through those queues. The core never awaits. `rtrb` has no disconnect signal, so ring shutdown is an explicit `AtomicBool` flag checked each poll-loop iteration, not channel-close semantics.

**M2 status:** Slice 2 (`feat/d1-fix-round-trip`) wired the first rings: `crates/d1` (new binary crate hosting the core thread + `d1-gateway-fix`) has two `rtrb` rings between the core thread and the FIX gateway (outbound `Order`, inbound `ExecEvent`). The core thread places one CLI-driven startup order as a stand-in for the netting-driven emit that lands in P1.M3 — there is no EXO target / netting yet. **Still deferred to Slice 3:** the feed-ingest ring and its producer thread — `MarketData::ingest` (`crates/d1-core/src/feed.rs`) stays a direct call until a real market-data transport shows up alongside `d1-gateway-nats`.

## The hot path contract (tick → order emit)

Target: 10–50 µs T2T inside the process, measured p50/p99/p99.9 with `criterion` + HDR histograms on a **release build** with pinned cores. Rules on any code reachable from the hot path:

1. **No heap allocation.** No `Box`, `Vec::push` beyond pre-reserved capacity, `String`, `format!`, or `clone()` of owning types. Pre-allocate at startup; use fixed-size arrays, arenas, or object pools. If you think you need an allocation, you need a design change.
2. **No locks.** No `Mutex`, `RwLock`, no `.lock()`. SPSC queues and single-writer state only. `Atomic*` with explicit ordering is allowed and must carry a comment justifying the ordering.
3. **No syscalls / no I/O / no logging.** Telemetry = write a fixed-size event into a preallocated ring consumed by a slow-path thread.
4. **No `async`.** Hot path is a poll loop (busy-spin or `spin_loop` hint).
5. **No panics.** `unwrap()`, `expect()`, `panic!`, indexing with `[]`, `unreachable!` are banned outside `#[cfg(test)]`. Use pattern matching and `get()`; unrepresentable states should be unconstructable via types.
6. **No `unsafe`** anywhere in this workspace without a dedicated ADR and a `// SAFETY:` comment. (The `quickfix` crate wraps C++ internally; that is confined to `d1-gateway-fix` and is off the hot path.)
7. **No floating point for money.** `price_e9: i64`, `qty_e2: i64` as defined in `protocol/proto/common.proto`. Overflow-checked arithmetic (`checked_add` etc.) at boundaries; `debug_assert!` internal invariants.

Enforcement: `cargo clippy` with `-D warnings` plus the lint set in `Cargo.toml` (`unwrap_used`, `expect_used`, `panic`, `indexing_slicing`, `float_arithmetic` allowed only in clearly marked non-hot modules).

## Rust guardrails for newcomers (this team is new to Rust)

- **Don't fight the borrow checker with `clone()` or `Rc<RefCell<..>>`.** If ownership is unclear, restructure: single owner, pass `&`/`&mut` down. Ask for a design review comment in the PR instead of sprinkling `clone()`.
- Prefer plain `struct` + `enum` + `match`. Avoid trait objects (`dyn Trait`) and generics-heavy designs until profiling or reuse demands them.
- `thiserror` for library errors, one error enum per crate; `anyhow` only in binaries. Never stringly-typed errors.
- Newtypes for identifiers: `BookId(u32)`, `InstrumentId(u32)`, `ClOrdId([u8; 20])`. Never pass bare `u32`/`&str` across module boundaries.
- All public items get `///` docs; `#![deny(missing_docs)]` on library crates.
- Tests live next to code (`#[cfg(test)] mod tests`); property tests with `proptest` for netting and order-state-machine logic are mandatory (netting bugs are silent money-losers).
- If a lifetime annotation gets complicated, stop and simplify the design. Complexity budget is spent on latency, not on clever generics.

## Approved dependency table (hot-path column is binding)

| Crate | Purpose | Allowed on hot path |
|-------|---------|---------------------|
| `rtrb` | SPSC rings (ADR-013) | yes |
| `prost` | Protobuf (gateway side) | no |
| `async-nats` | NATS client (official nats-io) | no |
| `quickfix` | FIX 4.4 engine (C++ binding) | no |
| `rdkafka` | Kafka producer | no |
| `criterion`, `hdrhistogram` | benches | test-only |
| `proptest` | property tests | test-only |
| `thiserror`, `anyhow` | errors | thiserror yes / anyhow no |
| `uuid` (v7) | msg ids | generated off hot path, pre-fetched pool on |
| `ctrlc` | Ctrl-C signal handler (`crates/d1`'s shutdown flag) | no -- registration happens once at startup on the main thread, never in a poll loop |

Anything not in this table needs a row added here + a sentence of justification in the PR description.

## Netting & internal crosses (summary — full spec in ADR-005)

- Inputs: per-book target positions (from EXO via NATS, and Delta One's own index-tracking targets), current per-book positions, in-flight orders.
- Per instrument: `net_external = Σ_book (target_book − position_book − inflight_book)`.
- The offsetting portion across books is booked as **explicit internal crosses**: two internal trades (one per book side) at the cross reference price, published to Kafka as `InternalCross` events, plus one external order for the residual. Worked example (verified): D1 book target +1,000,000 AAPL, EXO book target −800,000 → internal cross 800,000 (D1 buys from EXO internally, both legs booked), external order 200,000 buy.
- Cross reference price policy is configurable per instrument class (arrival mid default); it is a compliance-visible parameter, never hardcoded.
- External fills are allocated back to books pro-rata to residual demand; allocation events go to Kafka with full lineage (`parent ClOrdID`, fills, cross ids).

## Outbound planes

1. **NATS** (`d1-gateway-nats`): publish `ExecutionReport`, `PositionSnapshot`, `RiskSnapshot` (Protobuf) on subjects in `protocol/nats-subjects.md`; consume `TargetPosition` from EXO and `Command` request/reply from UI.
2. **FIX 4.4** (`d1-gateway-fix`): `NewOrderSingle` (35=D), `ExecutionReport` (35=8) inbound, `OrderCancelRequest` (35=F), `OrderCancelReplaceRequest` (35=G). Standard FIX 4.4 semantics only in Phase 1; venue dialects (EMSX/TSOX/FXAll) are thin adapter modules added later — their specs are proprietary and need firm entitlements, so the demo runs against `sim/`'s QuickFIX acceptor.
3. **Risk limits (Tier-1, ADR-008):** aggregate own delta with EXO-published Greeks per underlying, check against `d1.toml` limits (config, never code), publish `RiskLimitAlert` on `d1.alerts.limits`. Off hot path. Never auto-trades options on breach.
4. **Proposals & directed transfers (ADR-009):** cache active `HedgeProposal`s; on `ProposalDecision` validate (known, unexpired, same risk checks as any flow) then execute legs as ordinary orders for the proposing book; audit rejects with reason. `InternalTransferRequest` books a directed cross via `d1-netting`'s instruction entry point — same booking path, reference-price policy and records as netting-generated crosses.
5. **Kafka** (`d1-posttrade`): topics `posttrade.trades`, `posttrade.crosses`, `posttrade.allocations`, `posttrade.orders.audit`, `posttrade.tracker.analytics` (daily record, ADR-010) — Avro, Schema Registry, keyed by instrument for `trades` and by `cross_id` for crosses. At-least-once producer with idempotence enabled; consumers dedupe on `msg_id`.

## Definition of done for any delta-one PR

- clippy/fmt/tests green; no new lint allows.
- If the change touches `d1-core` or `d1-netting`: criterion bench diff pasted into the PR (p50/p99), and a proptest covering the new behavior.
- If the change touches a wire format: ADR + `just schema-check` green.
