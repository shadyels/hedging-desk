# ADR-010: Index-tracker analytics — tracking error, tracking difference, cash drag

**Status:** Accepted
**Date:** 2026-07-06
**Deciders:** desk lead (requested TE + cash drag for tracker books)

## Definitions (sourced)

- **Tracking error (ex-post):** the annualized standard deviation of daily
  return differences between the fund's total return and its underlying
  index's total return (ETF.com,
  https://www.etf.com/sections/etf-basics/understanding-tracking-difference-and-tracking-error).
  Annualization: period TE × sqrt(periods per year)
  (ryanoconnellfinance.com,
  https://ryanoconnellfinance.com/calculators/tracking-error-calculator/).
  Ex-post (realized, from history) vs ex-ante (model-predicted; used by
  managers to control risk) — Wikipedia,
  https://en.wikipedia.org/wiki/Tracking_error.
- **Tracking difference:** the cumulative return gap vs the benchmark over
  the window — drag magnitude, distinct from TE which is variability
  (ETF.com, same source).
- **Cash drag:** trackers hold cash unlike the index; the lag between
  receiving cash and reinvesting it causes deviation (AnalystPrep CFA L2,
  https://analystprep.com/study-notes/cfa-level-2/describe-sources-of-tracking-error-for-etfs/),
  reducing excess return especially in rising markets (AnalystPrep CFA L3,
  https://analystprep.com/study-notes/cfa-level-iii/tracking-error/).

## Decision

1. **Scope now (P1.M5): ex-post only.** Ex-ante TE needs a factor/risk model —
   deferred; named as a candidate ML item in ADR-011.
2. **Where:** new `d1-analytics` crate, off hot path. Inputs: per-book
   positions AND per-book cash (new position-keeper requirement), benchmark
   composition from `refdata/universe.json` `benchmarks` (DEMO placeholder
   composition until P4.M6), sim index/constituent ticks.
3. **Computation per tracker book (our operationalization of the sourced
   concepts — formulas below are our design):** sample book NAV return and
   benchmark return every `sampling_interval_s` (demo-time "day");
   TE = annualized stdev of active returns over `te_window_obs`;
   TD = cumulative active return over the window;
   cash drag = average cash weight × (benchmark return − cash return) over
   the window, cash return from the configured demo cash yield.
4. **Outputs:** `TrackerAnalytics` (Protobuf) on `d1.tracker.<book>` for the
   UI; daily Avro record on Kafka `posttrade.tracker.analytics`.
5. Config in `delta-one/d1.toml [tracker]`; all fractions `_e9` fixed point.

## Consequences

- Easier: the tracker-desk half of the demo gets a professional scorecard
  (TE/TD/cash-drag on the risk board) instead of positions only.
- Harder: position keeper must now track per-book cash from fills and sim
  dividend events; the sim gains a dividend event type (P1.M5).
- Demo honesty: benchmark composition and cash yield are labeled DEMO
  placeholders; TE quoted in the showcase is real math on synthetic data.
