# CLAUDE.md — Desk Hedging Platform (root)

Institutional hedging platform for index trackers and structured products.
Monorepo, five phases:

- **Phase 1 — Delta One (`delta-one/`, Rust):** linear hedging, high volume, latency-sensitive. Owns firm-wide netting, execution, FIX, post-trade publishing.
- **Phase 2 — EXO (`exo/`, Python):** exotics pricing (Monte Carlo, Heston / Heston-local-vol, PDE later). Computes target deltas per book and streams them to Delta One. EXO NEVER executes; it only publishes targets and consumes fills/risk.
- **Phase 3 — UI (`ui/`, TypeScript):** monitoring dashboard first, then manual order entry + kill switch + hedge-proposal approval.
- **Phase 4 — Advanced hedging & enrichment (MANDATORY, same depth):** Tier-2 hedge proposal engine, rho transfer to RATES-IR with external futures hedge (ADR-008/009), model depth (Heston-LV, PDE cross-check, synthetic-surface calibration), then universe enrichment at scale.
- **Phase 5 — optional, externally gated:** proprietary venue FIX dialects, live-market-data calibration, JetStream, AI assistance (ADR-011: LLM decision support + deep-hedging research, both non-executing).

**Before editing any component, read that component's own CLAUDE.md:**
`delta-one/CLAUDE.md`, `exo/CLAUDE.md`, `ui/CLAUDE.md`, `protocol/CLAUDE.md`, `sim/CLAUDE.md`. They contain non-obvious invariants; this file only holds cross-cutting rules.

## Documentation map — read these files when indicated (they are NOT auto-loaded)

| File | Read it BEFORE... |
|---|---|
| `docs/ARCHITECTURE.md` | any change touching data flow between components, threading, latency, failure/recon behavior, or the demo storyline |
| `docs/ROADMAP.md` | starting any new feature or milestone work, or judging whether something is in scope |
| `docs/adr/` (ADR-001…011) | touching anything an ADR governs: bus (001), post-trade (002), FIX (003), hot path (004), netting/crosses (005), models/products (006), tooling (007), Greeks ladder (008), proposals/transfers (009), tracker analytics (010), AI assistance (011, Proposed). ADRs are binding; read the relevant one in full |
| `protocol/nats-subjects.md` | publishing or subscribing to any NATS subject |
| `protocol/refdata/universe.json` | using any instrument_id, book_id, or market convention |
| `docs/GLOSSARY.md` | when a trading term in a task is unclear |
| `scripts/README.md` | implementing or debugging any `scripts/*.sh` (they are stubs; that file is their contract) |
| `deploy/docker-compose.yml` + `deploy/nats.conf` | changing local infra, ports, or NATS/Kafka/Schema-Registry config |

If a task matches a row above and the file hasn't been read this session, read it first. When docs and code disagree, flag it — do not silently pick one.

## Repo status: SCAFFOLD (read this before assuming a file exists)

This repo currently contains contracts, schemas, configs and docs — almost no implementation. Concretely:

- `scripts/*.sh` are failing stubs; their behavior contract is `scripts/README.md`.
- Rust crates (`delta-one/crates/*`, including the `sim` binary crate — sim's Rust code lives in the delta-one workspace per ADR-007, while `/sim/` holds its CLAUDE.md, scenarios and golden files) contain placeholder sources.
- Files referenced by component CLAUDE.md files as homes for specific logic (e.g. `exo/src/exo/bus/convert.py`, `ui/src/format.ts`, generated protobuf code) may not exist yet: they are contracts naming where that logic MUST live when you implement it, not files to search for. If a referenced file is missing, create it at exactly that path.
- Build order for implementation work is `docs/ROADMAP.md`; do not implement ahead of the current milestone.

## System topology (authoritative summary)

```
                    ┌────────────┐
 market data ─────▶ │  DELTA ONE │ ──FIX 4.4──▶ sim/ FIX acceptor (later: EMSX/TSOX/FXAll adapters)
                    │   (Rust)   │ ──Kafka(Avro)─▶ post-trade ledger topics
   NATS targets ──▶ │  netting + │ ──NATS──▶ execution reports, risk snapshots
        ▲           │  execution │
        │           └────────────┘
   ┌────┴────┐            ▲
   │   EXO   │            │ NATS (WebSocket listener for UI)
   │ (Python)│            ▼
   └─────────┘        ┌────────┐
                      │   UI   │  (nats.ws subscriber; command path via NATS request/reply)
                      └────────┘
```

- **EXO ↔ Delta One ↔ UI live plane:** NATS core pub/sub (Protobuf payloads). See ADR-001 for why NATS and not Aeron. Subject taxonomy: `protocol/nats-subjects.md`.
- **Post-trade plane:** Kafka, Avro + Schema Registry (ADR-002). Speed irrelevant here; correctness, replayability and schema evolution are the goals.
- **Execution plane:** FIX 4.4 via the `quickfix` Rust crate (ADR-003).
- **The 10–50 µs tick-to-trade budget applies ONLY inside the Delta One process** (market-data ingest → order emit, in-memory). No broker, no serialization, no syscall sits on that path. NATS/Kafka/FIX gateways all hang off the hot path via SPSC queues. See `docs/ARCHITECTURE.md#latency-budget`.

## Cross-cutting invariants (violating any of these is a bug)

1. **Money and quantities are never `f64`/`float`/`number` at rest or on the wire.** Prices are `i64` in fixed-point (scale in `common.proto`: `price_e9`, i.e. ×10⁻⁹); quantities are `i64` in units-e2 where fractional sizes exist. Floating point is allowed only inside pricing math in EXO and is converted at the bus boundary.
2. **Every position, target, order, fill and cross carries `book_id`.** Netting is computed firm-wide but attribution is per book; internal crosses are booked explicitly as two offsetting internal trades (ADR-005). Never "net away" a position silently.
3. **All bus messages are schema'd.** NATS payloads: Protobuf in `protocol/proto/`. Kafka payloads: Avro in `protocol/avro/`. Never publish ad-hoc JSON on either plane. Schema changes follow `protocol/CLAUDE.md` (backward-compatible only; additive fields; never renumber/reuse Proto tags).
4. **Idempotency:** every message has a `msg_id` (UUIDv7) and producers may redeliver. Consumers must dedupe. Order state transitions are driven by `ClOrdID`/`ExecID`, never by message arrival count.
5. **EXO output is a *target*, not an order.** Delta One decides what (if anything) to execute after netting and risk checks.
6. **UI is read-mostly.** Any UI-initiated action goes through NATS request/reply to Delta One's command handler, which applies the same risk checks as any other flow. The UI never talks to Kafka or FIX directly.
7. **Determinism in EXO:** every pricing run records `(model_id, model_params, seed, n_paths, git_sha)` so a number can be reproduced. Published Greeks without this metadata are invalid.

## Build / run / test

Task runner is `just` (see `justfile`). Top-level targets:

```
just up            # docker compose: NATS, Kafka (KRaft), Schema Registry
just proto         # regenerate Rust/Python/TS code from protocol/
just d1            # build + run delta-one (debug)
just d1-release    # build with release profile (the only profile for latency tests)
just exo           # run EXO service
just ui            # run UI dev server
just sim           # run market-data generator + FIX acceptor
just test          # all unit tests (Rust + Python + TS)
just bench         # criterion benches for the hot path (release only)
just demo          # full stack end-to-end demo scenario
```

Never benchmark or make latency claims from a debug build.

## Repo conventions

- **Branch names:** `type/scope-description` (e.g. `feat/d1-netting`, `fix/exo-calibration`, `docs/adr-008`, `chore/hooks-n-conventions`). Use component names (d1, exo, ui, protocol, sim) or workflow types (feat, fix, chore, docs, refactor). Keep under 50 chars. Delete after merge.
- Conventional Commits (`feat(d1-netting): ...`). One logical change per commit.
- CI gates (all must pass before merge): `cargo clippy -- -D warnings`,`cargo test`, `cargo fmt --check`, `ruff check`, `ruff format --check`, `mypy --strict`, `pytest`, `tsc --noEmit`, `eslint`, schema-compat checks(`just schema-check`).
- ADRs live in `docs/adr/`. Any decision that constrains another component (wire format, subject name, latency budget, model choice) requires an ADR before code. Existing ADRs are binding unless superseded.
- Do not add dependencies casually. Rust: any new crate on the hot path needs a note in `delta-one/CLAUDE.md`'s dependency table. Python: add to `pyproject.toml` with a version pin.
- Scope principle: the **instrument universe** starts minimal during development and is enriched at scale in P4.M5 — but **project scope is extensive**; Phases 1–4 are mandatory at equal depth. Never confuse data minimalism with feature minimalism (`docs/ROADMAP.md` is authoritative).

## What Claude (or any agent) must NOT do in this repo

- Introduce `unsafe` Rust, `unwrap()`/`expect()` outside tests, or heap allocation on the hot path (full list in `delta-one/CLAUDE.md`).
- Change a Proto field number, Avro field type, or NATS subject name without an ADR + schema-compat check.
- Put pricing logic in Delta One or execution logic in EXO.
- Invent market conventions (day counts, settlement lags, tick sizes) — they are defined once in `protocol/` reference data and consumed everywhere.
- Implement ahead of the current `docs/ROADMAP.md` milestone or widen the development universe before P4.M5 (milestone ordering discipline, not scope reduction).
- Commit or push directly to `main`; all changes go via a `type/scope-description` branch and a reviewed PR.
