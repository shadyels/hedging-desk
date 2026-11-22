/**
 * Pure tracker-scorecard logic: wire guard, display labels, sparkline geometry. No React, no clock.
 * Wire quantities stay bigint; `Number` appears only on a value already reduced to pixels or a clock delta.
 */
import type { WithMeta } from "../bus/subjects";
import { TrackerAnalytics, TrackerAnalytics_Kind } from "../gen/live";
import { MISSING } from "../format";
import type { Entry } from "../store";

export const asTracker = (v: WithMeta | undefined): TrackerAnalytics | null =>
  v && "trackingErrorAnnE9" in v && typeof v.trackingErrorAnnE9 === "bigint"
    ? (v as TrackerAnalytics)
    : null;

export function kindLabel(k: TrackerAnalytics_Kind): string {
  if (k === TrackerAnalytics_Kind.KIND_EX_POST) return "ex-post";
  if (k === TrackerAnalytics_Kind.KIND_EX_ANTE) return "ex-ante";
  return MISSING;
}

/**
 * ponytail: TrackerAnalytics has no demo flag and Meta only names the producer, so the DEMO badge is
 * this constant, not wire data. ADR-010: benchmark composition and cash yield are DEMO placeholders
 * until P4.M6. Delete it (or read a wire flag, if one is added) when real composition lands.
 */
export const TRACKER_BASIS_IS_DEMO = true;
export const DEMO_TITLE =
  "Benchmark composition and cash yield are DEMO placeholders until P4.M6 (ADR-010).";

/** What the card says; the ADR-010 quote below is its tooltip. */
export const PRICE_ONLY_SHORT =
  "Price-only benchmark: dividends land in tracking difference.";

/** ADR-010 "Demo honesty", quoted. Same text on every card. */
export const PRICE_ONLY_NOTE =
  "Price-only benchmark: the benchmark return is price-only while book NAV is total return, so constituent dividends land entirely in tracking difference (ADR-010).";

/**
 * window_start_ns / window_end_ns come from Delta One's synthetic feed clock (delta-one/CLAUDE.md), so
 * they are not wall-clock times. Only their difference is meaningful: returned in ms, null if unusable.
 */
export function windowSpanMs(m: TrackerAnalytics): number | null {
  if (m.windowEndNs < m.windowStartNs || m.windowEndNs === 0n) return null;
  const ms = (m.windowEndNs - m.windowStartNs) / 1_000_000n;
  return ms > BigInt(Number.MAX_SAFE_INTEGER) ? null : Number(ms);
}

export interface SparkSample {
  /** Arrival time (ms, UI clock): the x axis. */
  t: number;
  /** tracking_error_ann_e9. */
  v: bigint;
}

/** TE series of one book, oldest first, from its ring. Entries that are not TrackerAnalytics are skipped. */
export function teSeries(ring: readonly Entry[] | undefined): SparkSample[] {
  const out: SparkSample[] = [];
  for (const e of ring ?? []) {
    const m = asTracker(e.value);
    if (m) out.push({ t: e.receivedAtMs, v: m.trackingErrorAnnE9 });
  }
  return out;
}

export interface SparkPoint {
  x: number;
  y: number;
}

/**
 * Sparkline geometry in a w x h box with `pad` px inset. x is linear in arrival time (a gap in the
 * feed shows as a gap); y is linear in value between the series' own min and max, with NO zero
 * baseline and no scale labels, so the line makes no claim about magnitude. Min is at the bottom.
 * A flat series sits on the middle line. One sample, or samples that share a timestamp, sit at the
 * right edge (the newest end). All output is finite. y is computed in bigint, in 1/100 px.
 */
export function sparkPoints(
  s: readonly SparkSample[],
  w: number,
  h: number,
  pad = 1,
): SparkPoint[] {
  const first = s[0];
  const last = s[s.length - 1];
  if (!first || !last) return [];
  const innerW = Math.max(0, w - 2 * pad);
  const innerH = Math.max(0, h - 2 * pad);
  let min = first.v;
  let max = first.v;
  for (const p of s) {
    if (p.v < min) min = p.v;
    if (p.v > max) max = p.v;
  }
  const range = max - min;
  const span = last.t - first.t;
  const scale = BigInt(Math.round(innerH * 100));
  return s.map((p) => {
    const x = span > 0 ? pad + ((p.t - first.t) / span) * innerW : w - pad;
    const y =
      range === 0n ? h / 2 : pad + Number(((max - p.v) * scale) / range) / 100;
    return { x, y };
  });
}

export const pointsAttr = (pts: readonly SparkPoint[]): string =>
  pts.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");
