# ADR-005: Firm-wide netting with explicit per-book internal crosses

**Status:** Accepted
**Date:** 2026-07-05
**Deciders:** desk lead (per-book attribution + explicit cross booking confirmed)

## Context

Multiple books (Delta One tracker books, EXO structured-product books) express target positions per instrument. Executing each book's demand independently wastes spread and market impact; netting silently would destroy per-book attribution and is a compliance problem — internal transfers of risk between books must be visible, booked trades.

## Decision

Per instrument, per netting cycle:

1. Compute each book's residual demand:
   `demand_b = target_b − position_b − inflight_b`.
2. `net_external = Σ_b demand_b` → one external order (or none if inside the instrument's no-trade band).
3. The offsetting portion `Σ_b max(demand_b,0) − max(net_external,0)` (long
   side; symmetric short side) is matched across books and booked as
   **internal crosses**: for each matched pair, two internal trade records
   (buy leg book_x, sell leg book_y), a shared `cross_id`, the cross reference
   price, and full lineage — published on `posttrade.crosses`.
4. Cross reference price: policy per instrument class, default **arrival mid** at cycle start; configurable (e.g., execution VWAP of the concurrent external residual). The policy id is stamped on every cross record. This is a compliance-visible parameter: fairness between books depends on it, and it must never be hardcoded or changed without an ADR.
5. External fills are allocated pro-rata to residual demand per book;
   `posttrade.allocations` records parent `ClOrdID` → book quantities.

Worked example (numbers script-verified): book D1-CORE demand +1,000,000 AAPL, book EXO-SP demand −800,000 → internal cross 800,000 (D1-CORE buys from EXO-SP internally, two booked legs) + external buy order 200,000; on fill, the entire 200,000 allocates to D1-CORE.

Matching-pair order when >2 books: largest-opposite-first, deterministic tie-break by `book_id` — determinism is required for the golden-file end-to-end test.

## Consequences

- Easier: audit and compliance story ("every risk transfer is a booked trade"); per-book P&L stays exact; the demo's flagship narrative.
- Harder: netting engine is genuinely stateful (in-flight tracking, partial fills against netted parents) — this is the hardest correctness surface in Phase 1; mandatory property tests (`Σ book positions == firm position` under arbitrary fill/cancel interleavings) per `delta-one/CLAUDE.md`.
- Open question deliberately deferred: cross timing vs external execution (cross at cycle start vs after external completes). Demo: cross at cycle start at arrival mid. Revisit with the desk's compliance function before any production use.
