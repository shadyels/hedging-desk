# CLAUDE.md — sim (market data generator + FIX counterparty)

Everything fake lives here so nothing fake lives anywhere else.
The demo is fully self-contained: no real connectivity.

Code location: sim's Rust binary is the `sim` crate in the `delta-one/`
Cargo workspace (ADR-007); this directory holds scenarios (`scenarios/`),
golden outputs (`golden/`, created in P1.M4) and these rules.

## Components

1. **Market data generator** (Rust binary, shares `d1` message types):
   publishes ticks for the demo universe. Modes:
   - `replay` — deterministic scripted scenarios (the demo runs on these),
   - `random-walk` — correlated GBM with configurable vol/correlation for
     soak testing,
   - `burst` — throughput stress for latency measurement.
   Feeds Delta One over the same in-process/UDP path the real feed handler
   will use — the hot-path measurement must include the ingest boundary.
2. **FIX acceptor** (QuickFIX-based): accepts FIX 4.4 sessions from
   `d1-gateway-fix`, fills orders per a configurable fill model
   (immediate / partial / delayed / reject rate), emits ExecutionReports.
   This stands in for a broker/EMS; venue-dialect adapters (EMSX, TSOX,
   FXAll) come only when real specs and entitlements exist.
3. **Scenario runner:** YAML scenarios that script market moves + EXO book
   events for the showcase (e.g., "underlying gaps down through autocall
   barrier → EXO delta jumps → cross vs D1 tracker flow → residual executes
   externally → post-trade lands in Kafka → UI shows lineage"). The demo
   story lives here as code, not in someone's head.

## Rules

- Deterministic by default: every scenario has a seed; two runs of the same
  scenario produce byte-identical Kafka output (this is also the end-to-end
  regression test: `just demo` diffs the post-trade stream against a golden
  file).
- The simulator may be sloppy about latency but never about protocol
  correctness: FIX session behavior (sequence numbers, resend, logon/logout)
  must be spec-correct, because Delta One's session handling is being tested
  against it.
- No sim code may be imported by production crates/packages; dependency
  direction is sim → protocol only.
