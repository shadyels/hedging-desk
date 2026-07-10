# CLAUDE.md — ui (TypeScript)

Phase 3. Monitoring dashboard first; manual order entry + kill switch second.
Read the root `CLAUDE.md` first.

## Stack

- React 19 + Vite + TypeScript (`strict: true`, no `any`, no `@ts-ignore`).
- **Data plane:** the UI is a NATS client over WebSocket using `nats.ws` (NATS supports WebSocket natively — no bespoke gateway service needed for the demo; the compose file exposes a WS listener on the NATS server). Payloads are the same Protobuf messages as everywhere else, decoded with generated `ts-proto` code from `protocol/proto/` — never hand-decode, never parallel JSON shapes.
- State: TanStack Query is wrong for push data; use a thin subscription store (Zustand) fed by NATS subscriptions, with per-subject ring buffers.
- Charts/tables: virtualized tables for blotters (positions, orders, fills);
  latency and P&L sparklines. Don't render 10k rows to the DOM.

## Screens (build in this order)

1. **Desk risk board:** per-book and firm-net delta by underlying; EXO target vs current position vs in-flight; rehedge band visualization; tracker scorecard per index book — TE, tracking difference, cash drag with limit coloring (`d1.tracker.<book>`, ADR-010).
2. **Order/execution blotter:** live order state machine view keyed by `ClOrdID`, fills, internal crosses shown explicitly as first-class rows (they are booked trades, not footnotes).
3. **System health:** NATS lag, last EXO valuation metadata (model, seed, n_paths, git_sha — surfacing invariant #7 from root CLAUDE.md), Kafka producer status, FIX session state.
4. **Actions (P3.M2): manual ticket, kill switch, hedge-proposal approve/reject:** render `HedgeProposal` with pre/post exposure, modeled cost, validity countdown; approve/reject via `d1.cmd.proposal` with mandatory reject reason. All actions go over NATS request/reply `d1.cmd.*` and require a typed ack from Delta One. The UI must render the *rejection reason* path as carefully as the success path. Kill switch is a command to Delta One; the UI never fakes state locally.

## Rules

1. Read-mostly by design: no business logic, no netting math, no P&L
   computation client-side beyond display aggregation. If a number matters, it
   is computed upstream and shipped on the bus.
2. Fixed-point discipline extends here: `price_e9`/`qty_e2` arrive as
   `bigint` (ts-proto `forceLong=bigint`); format at the edge with a single
   shared `format.ts`. Never convert to `number` before arithmetic.
3. Every subject subscribed must exist in `protocol/nats-subjects.md`.
4. Reconnect logic: on NATS disconnect, grey out the whole board (stale data is worse than no data on a trading desk); resubscribe + request fresh snapshots on reconnect.
5. Component tests with Vitest + Testing Library; the order-state rendering
   and the disconnect/stale behavior must have tests.
6. `eslint` + `prettier` enforced in CI; `tsc --noEmit` gate.
