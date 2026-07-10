# Desk Hedging Platform

Institutional hedging platform for index trackers and structured products.
Demo/showcase system — fully self-contained (no real market connectivity).

**If you are an AI agent or a new contributor: start with `CLAUDE.md` (root), then the CLAUDE.md of the component you're touching. The ADRs in `docs/adr/` are binding.**

## Quick start

```bash
just up      # NATS + Kafka + Schema Registry
just proto   # regenerate codegen (once implemented, P1.M1)
just demo    # full end-to-end showcase scenario
```

## Layout

| Path | What | Language |
|---|---|---|
| `delta-one/` | Phase 1: linear hedging, netting, execution, FIX, post-trade | Rust |
| `exo/` | Phase 2: exotics pricing (MC/Heston), target-delta publication | Python |
| `ui/` | Phase 3: monitoring dashboard, then manual ticket + kill switch | TypeScript |
| `protocol/` | wire contracts: Protobuf (NATS), Avro (Kafka), subjects, refdata | — |
| `sim/` | market-data generator, FIX acceptor, demo scenarios | Rust/YAML |
| `docs/` | architecture, roadmap, ADRs, glossary | — |
| `deploy/` | docker compose for local infra | — |

## The one-paragraph design

EXO prices the structured-product book (Monte Carlo under Heston / Heston-LV)
and publishes per-book **target positions** over NATS. Delta One nets demand
firm-wide across books, books the offsetting portion as **explicit internal
crosses** (per-book attribution preserved), executes only the residual over
**FIX 4.4**, and publishes booked trades/crosses/allocations to **Kafka
(Avro)** for the post-trade ledger. The 10–50 µs tick-to-trade budget lives
entirely inside the Delta One process; all buses are off the hot path by
design (ADR-004). Phase 4 (mandatory) adds the advanced layer: a full vega/liquidity hedge
optimizer producing trader-approved option **HedgeProposals**, and directed
**rho transfers** to a rates book that hedges externally through the same
pipeline (ADR-008/009). The UI subscribes to NATS over WebSocket.
