# PRODUCT.md — Desk Hedging monitor (ui, P3.M1)

Product truth only. Visual decisions live in `DESIGN.md`. Scope and order of work live in `CLAUDE.md` and `docs/ROADMAP.md` (P3).

## What it is

A desktop monitoring terminal for the Desk Hedging Platform. It shows what Delta One and EXO publish on NATS, as it happens, and nothing else. It is the desk's single place to answer "is the book inside its limits, is the hedge doing what EXO asked, and can I trust the numbers on screen right now".

It is **read-mostly**. Every number is computed upstream and shipped on the bus; the UI formats and aggregates for display only (`ui/CLAUDE.md` rule 1).

## Who uses it

| User                           | Sits with it                                    | Cares about                                                                                                                              |
| ------------------------------ | ----------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| **Desk risk / hedging trader** | All day, one monitor, glances every few seconds | Limit state per underlying and Greek, headroom to hard limit, EXO target vs position vs in-flight, whether a book has stopped publishing |
| **Tracker PM** (index books)   | Several times a day                             | Tracking error, tracking difference, cash drag per tracker book (ADR-010); whether the numbers are fresh                                 |
| **Ops / support**              | When something looks wrong                      | Order and cross lineage by `ClOrdID`, fills and allocations, component health, which git_sha produced a valuation                        |

## Jobs to be done, per screen

1. **Risk board.** Spot a limit moving toward WARN or HARD before it trips, and see which underlying and Greek it is. See per-book contribution and whether a book is halted or stale. Compare EXO target, current position and in-flight quantity and see whether the gap is inside the rehedge band. Read the alert history of the session.
2. **Tracker scorecard.** See each tracker book's TE, TD and cash drag at a glance, their recent trend, and whether a card is fresh, stale or has never reported.
3. **Blotter.** Follow an order from netting parent to per-book allocations and fills by `ClOrdID`. Find an internal cross and see which books it connected and at what reference price policy. Filter by book, instrument, kind and status. Stay usable at thousands of rows.
4. **System health.** Confirm each component is alive. See each book's EXO state (publishing, awaiting snapshot or marks, halted). Read the last valuation metadata (model, params hash, seed, n_paths, git_sha) and be warned when valuations come from different git_sha values (root invariant 7).

## Product principles

- **Stale is worse than empty.** The UI never shows an old number as if it were live, and never shows a placeholder zero for a number that has not arrived. Waiting, stale and disconnected are three different, visible states.
- **Honest about what the bus does not say.** Alerts are sent on level change only; exec messages are a stream with no snapshot. The UI says "since connect" and never claims "none" when it only means "not seen".
- **Colour means limit or health, nothing else.** Everything else is neutral ink.
- **Density over decoration.** A trader reads 100+ numbers per screen. Alignment and rhythm do the work chrome would do elsewhere.

## Non-goals (P3.M1)

- **No order entry, no kill switch, no hedge-proposal approve/reject.** Those are P3.M2 and go over `d1.cmd.*` request/reply with typed acks.
- No client-side netting, P&L or Greek computation; no limit evaluation (levels come from Delta One).
- No mobile or tablet layout. Desktop, minimum width 1280 px.
- No historical queries, charts of arbitrary history, reports, or export. Sparklines and the alert ticker are ring buffers of what arrived this session.
- No authentication or user management (NATS has none today; owner P3.M2, ROADMAP).
- No Kafka or FIX access from the browser, ever (root invariant 6).

## Data it consumes

All subjects exist in `protocol/nats-subjects.md`: `d1.risk.firm`, `d1.alerts.limits`, `d1.positions.<book>`, `exo.targets.<book>.<instrument>`, `exo.valuations.<book>`, `exo.alerts.recon`, `d1.exec.<book>.<instrument>`, `d1.crosses`, `d1.tracker.<book>`, `d1.health`, `exo.health`, `d1.marks.<instrument>` (display only).
