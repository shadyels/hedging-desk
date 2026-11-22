# Desk Hedging Platform

Institutional hedging platform for index trackers and structured products. Demo/showcase system — fully self-contained (no real market connectivity).

**If you are an AI agent or a new contributor: start with `CLAUDE.md` (root), then the CLAUDE.md of the component you're touching. The ADRs in `docs/adr/` are binding.**

## Status

Milestone status as recorded in `docs/ROADMAP.md`:

| Phase | Milestones | Status |
|---|---|---|
| 1 — Delta One (Rust) | Skeleton & contracts, Order path, Netting & crosses, Post-trade, Tracker analytics (ADR-010) | Met (two stated limitations) |
| 2 — EXO (Python) | Engine + first products, Equity product set, FX products, The loop | Met (stated limitations per milestone) |
| 3 — UI (TypeScript) | P3.M1 Monitoring; P3.M2 Actions | P3.M1 in progress; P3.M2 not started |
| 4 — Advanced hedging & enrichment | P4.M1 Rates foundation; P4.M2 Rho transfer; P4.M3 Hedge proposal engine; P4.M4 Model depth; P4.M5 Multi-asset; P4.M6 Universe enrichment at scale | Not started |
| 5 — Optional, externally gated | Proprietary venue FIX dialects, live-market calibration, JetStream evaluation, AI assistance (ADR-011, Proposed), rough-volatility research | Not started |

## Prerequisites

- Rust 1.88.0 (pinned in `delta-one/rust-toolchain.toml`)
- Python 3.13 with [uv](https://docs.astral.sh/uv/)
- Node.js with npm (no pinned version)
- Docker, with the daemon running (needed by `just up`, `just storyline`, `just demo`)
- [just](https://github.com/casey/just) as the task runner
- `buf`, `protoc` 29.x and `protoc-gen-prost` 0.5.0, only for `just proto`

## Quick start

```bash
just setup-hooks                        # once per clone: local guard against direct commits to main
(cd exo && uv sync --all-extras --dev)  # once: Python dependencies
(cd ui && npm install)                  # once: UI dependencies
just up                                 # NATS, Kafka, Schema Registry (docker compose)
just test                               # cargo test, pytest, vitest
just demo                               # the six infra-gated integration tests
```

Running the pieces by hand:

```bash
just sim-acceptor    # FIX counterparty for d1 (terminal 1, keep running)
just d1              # place one startup order over FIX (terminal 2)
just exo             # EXO: value the book and publish targets
just ui              # dashboard dev server (Vite)
just storyline       # tracker-flow storyline across d1 and EXO (needs `just up`)
```

Caveats:

- `just d1` and `just d1-scenario` need `just sim-acceptor` running first.
- `just exo` starts on a dirty git tree, but EXO refuses to publish targets there (`[provenance] allow_dirty = false`). Commit first.
- `just storyline` needs `just up` and a quiet bus: it refuses to start if it sees other EXO/D1 traffic. Do not run it alongside `just exo`, `just d1*`, or another infra test.
- `just demo` runs `just up` at start and `just down` at exit, so it tears down a stack you started yourself. `just down` runs `docker compose down -v`, which deletes volumes. `just demo` runs the six infra-gated tests in sequence; it is not a UI demo, and the UI is not left running.
- `just proto` needs the tools listed under Prerequisites and `npm install` in `ui/`. Ordinary builds do not: generated code is committed.
- `just schema-check` checks the Avro schemas against a running Schema Registry, so run `just up` first.

## Layout

| Path | What | Language |
|---|---|---|
| `delta-one/` | Phase 1 Rust workspace: crates `d1` (binary: core thread and gateways), `d1-core`, `d1-netting`, `d1-refdata`, `d1-analytics`, `d1-posttrade`, `d1-gateway-fix`, `d1-gateway-nats`, and `sim` (sim's Rust code, ADR-007) | Rust |
| `exo/` | Phase 2 exotics pricing (MC/Heston) and target-delta publication: `src/exo/` holds `models/`, `products/` (autocallable, barrier, bonus, reverse_convertible, warrant, mini_future, tarf), `greeks/`, `portfolio/`, `bus/`, `studies/`, `provenance.py` | Python |
| `ui/` | Phase 3 dashboard over nats.ws: `src/` screens `risk/`, `tracker/`, `blotter/`, `health/`. Order entry and kill switch are P3.M2, not built | TypeScript |
| `protocol/` | wire contracts: Protobuf (NATS), Avro (Kafka), subjects, refdata | — |
| `sim/` | CLAUDE.md, scenarios, golden files (the Rust code is in `delta-one/crates/sim`) | YAML |
| `scripts/` | `gen-proto.sh`, `schema-check.sh`, `demo.sh` (contracts in `scripts/README.md`) | Bash |
| `.github/workflows/` | CI: `rust`, `python`, `ui`, `schema-compat`, `infra` | — |
| `docs/` | architecture, roadmap, ADRs, glossary | — |
| `deploy/` | docker compose and NATS config for local infra | — |

## The one-paragraph design

EXO prices the structured-product book (Monte Carlo under Heston / Heston-LV) and publishes per-book **target positions** over NATS. Delta One nets demand firm-wide across books, books the offsetting portion as **explicit internal crosses** (per-book attribution preserved), executes only the residual over **FIX 4.4**, and publishes booked trades/crosses/allocations to **Kafka (Avro)** for the post-trade ledger. The 10–50 µs tick-to-trade budget lives entirely inside the Delta One process; all buses are off the hot path by design (ADR-004). Phase 4 (mandatory) adds the advanced layer: a full vega/liquidity hedge optimizer producing trader-approved option **HedgeProposals**, and directed **rho transfers** to a rates book that hedges externally through the same pipeline (ADR-008/009). The UI subscribes to NATS over WebSocket.

## Docs map

- `docs/ARCHITECTURE.md` — data flow, latency budget, failure and reconciliation behaviour, demo storyline
- `docs/ROADMAP.md` — milestones and exit criteria; the authoritative build order
- `docs/GLOSSARY.md` — trading terms
- `docs/PONYTAIL-DEBT.md` — deliberate shortcuts and their phase-gated upgrade paths
- `docs/GATE-MARGIN-AUDIT.md` — audit of EXO validation-gate statistical power
- `docs/adr/` — ADR-001 to ADR-014 (binding; ADR-011 is Proposed)
