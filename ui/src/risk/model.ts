/**
 * Pure risk-board logic: wire guards, limit mapping, headroom, target/position join, valuation roll-up.
 * No React, no clock. Everything on a wire quantity is bigint (ui/CLAUDE.md rule 2); `Number` appears
 * only where a bigint has already been reduced to a presentation value (headroom basis points).
 */
import type { ChipState } from "../components/StateChip";
import type { WithMeta } from "../bus/subjects";
import {
  RiskSnapshot_LimitLevel as Level,
  type PositionSnapshot,
  type RiskLimitAlert,
  type RiskSnapshot,
  type TargetPosition,
  type ValuationSnapshot,
} from "../gen/live";
import type { Entry } from "../store";

// ---- Guards: the store holds WithMeta; each panel reads one subject, so a field check is enough ----

export const asRisk = (v: WithMeta | undefined): RiskSnapshot | null =>
  v && "greeks" in v && Array.isArray(v.greeks) ? (v as RiskSnapshot) : null;

export const asTarget = (v: WithMeta | undefined): TargetPosition | null =>
  v && "targetQtyE2" in v && typeof v.targetQtyE2 === "bigint"
    ? (v as TargetPosition)
    : null;

export const asPositions = (
  v: WithMeta | undefined,
): PositionSnapshot | null =>
  v && "lines" in v && Array.isArray(v.lines) && !("bookPvE9" in v)
    ? (v as PositionSnapshot)
    : null;

export const asValuation = (
  v: WithMeta | undefined,
): ValuationSnapshot | null =>
  v && "bookPvE9" in v && Array.isArray((v as { lines?: unknown }).lines)
    ? (v as ValuationSnapshot)
    : null;

export const asAlert = (v: WithMeta | undefined): RiskLimitAlert | null =>
  v && "cleared" in v && typeof v.cleared === "boolean"
    ? (v as RiskLimitAlert)
    : null;

// ---- Limit level -> chip state --------------------------------------------------------------

export type LimitChipState = Extract<
  ChipState,
  "ok" | "warn" | "hard" | "waiting"
>;

/** UNSPECIFIED (not populated, ADR-014 Amend 1 s1) and any unknown value read WAITING, never OK. */
export function limitChipState(level: Level): LimitChipState {
  switch (level) {
    case Level.LIMIT_LEVEL_OK:
      return "ok";
    case Level.LIMIT_LEVEL_WARN:
      return "warn";
    case Level.LIMIT_LEVEL_HARD:
      return "hard";
    default:
      return "waiting";
  }
}

// ---- Headroom -------------------------------------------------------------------------------

export type Headroom =
  | { kind: "na" }
  | { kind: "bar"; fillBps: number; warnBps: number | null; over: boolean };

/**
 * |value| / hard cap as a bar. Compare and divide in bigint first; only the final 0..10000 basis-point
 * figure (a presentation width, not money) is converted to `number`. A missing cap (<= 0) is "na".
 * The warn tick is omitted if the warn cap is missing or above the hard cap.
 */
export function headroom(value: bigint, warn: bigint, hard: bigint): Headroom {
  if (hard <= 0n) return { kind: "na" };
  const mag = value < 0n ? -value : value;
  const bps = (x: bigint) => Number((x * 10_000n) / hard);
  return {
    kind: "bar",
    fillBps: bps(mag > hard ? hard : mag),
    warnBps: warn > 0n && warn <= hard ? bps(warn) : null,
    over: mag > hard,
  };
}

// ---- Target / position / in-flight join -----------------------------------------------------

export interface BandRow {
  bookId: number;
  instrumentId: number;
  symbol: string;
  target: bigint | null;
  band: bigint | null;
  targetAtMs: number | null;
  position: bigint | null;
  inflight: bigint | null;
  positionAtMs: number | null;
}

/** target - position, bigint, no clamp. null unless both exist: never a placeholder zero. */
export const gapQtyE2 = (
  target: bigint | null,
  position: bigint | null,
): bigint | null =>
  target === null || position === null ? null : target - position;

/** |gap| <= band (EXO's authoritative no-trade half-width, ADR-014 s11). null if either is unknown. */
export function insideBand(gap: bigint | null, band: bigint | null) {
  if (gap === null || band === null) return null;
  return (gap < 0n ? -gap : gap) <= band;
}

const rowKey = (book: number, ins: number) => `${book}:${ins}`;

export function joinTargetsPositions(
  targets: Record<string, Entry>,
  positions: Record<string, Entry>,
): BandRow[] {
  const rows = new Map<string, BandRow>();
  const get = (
    bookId: number,
    instrumentId: number,
    symbol: string,
  ): BandRow => {
    const k = rowKey(bookId, instrumentId);
    let r = rows.get(k);
    if (!r) {
      r = {
        bookId,
        instrumentId,
        symbol,
        target: null,
        band: null,
        targetAtMs: null,
        position: null,
        inflight: null,
        positionAtMs: null,
      };
      rows.set(k, r);
    }
    return r;
  };
  for (const e of Object.values(targets)) {
    const t = asTarget(e.value);
    if (!t) continue;
    const r = get(
      t.bookId,
      t.instrument?.instrumentId ?? 0,
      t.instrument?.symbol ?? "",
    );
    r.target = t.targetQtyE2;
    r.band = t.bandQtyE2;
    r.targetAtMs = e.receivedAtMs;
  }
  for (const e of Object.values(positions)) {
    const p = asPositions(e.value);
    if (!p) continue;
    for (const l of p.lines) {
      const r = get(
        p.bookId,
        l.instrument?.instrumentId ?? 0,
        l.instrument?.symbol ?? "",
      );
      r.position = l.qtyE2;
      r.inflight = l.inflightQtyE2;
      r.positionAtMs = e.receivedAtMs;
    }
  }
  return [...rows.values()].sort(
    (a, b) =>
      a.symbol.localeCompare(b.symbol) ||
      a.instrumentId - b.instrumentId ||
      a.bookId - b.bookId,
  );
}

// ---- Valuations: one row per (book, underlying) --------------------------------------------

export interface ValuationRow {
  bookId: number;
  instrumentId: number;
  /** "" when the book published no product lines. */
  symbol: string;
  model: string;
  nPaths: number;
  seed: bigint;
  delta: bigint;
  vega: bigint;
  gamma: bigint;
  rho: bigint;
  theta: bigint;
  divSens: bigint;
  flagged: string[];
  receivedAtMs: number;
  hasLines: boolean;
}

/**
 * Lines sum to book totals per underlying (ADR-014 s9: desk-position values, one currency per
 * underlying). Summing across underlyings would mix units, so the roll-up stays per underlying.
 */
export function valuationRows(entries: Record<string, Entry>): ValuationRow[] {
  const out: ValuationRow[] = [];
  for (const e of Object.values(entries)) {
    const v = asValuation(e.value);
    if (!v) continue;
    const base = {
      bookId: v.bookId,
      model: v.valuation?.modelId ?? "",
      nPaths: v.valuation?.nPaths ?? 0,
      seed: v.valuation?.seed ?? 0n,
      receivedAtMs: e.receivedAtMs,
    };
    const zero = {
      delta: 0n,
      vega: 0n,
      gamma: 0n,
      rho: 0n,
      theta: 0n,
      divSens: 0n,
    };
    if (v.lines.length === 0) {
      out.push({
        ...base,
        ...zero,
        instrumentId: 0,
        symbol: "",
        flagged: [],
        hasLines: false,
      });
      continue;
    }
    const by = new Map<number, ValuationRow>();
    for (const l of v.lines) {
      const id = l.underlying?.instrumentId ?? 0;
      let r = by.get(id);
      if (!r) {
        r = {
          ...base,
          ...zero,
          instrumentId: id,
          symbol: l.underlying?.symbol ?? "",
          flagged: [],
          hasLines: true,
        };
        by.set(id, r);
      }
      r.delta += l.deltaQtyE2;
      r.vega += l.vegaE9;
      r.gamma += l.gammaQtyE2;
      r.rho += l.rhoE9;
      r.theta += l.thetaE9;
      r.divSens += l.divSensE9;
      for (const g of l.flaggedGreeks)
        if (!r.flagged.includes(g)) r.flagged.push(g);
    }
    for (const r of by.values()) {
      r.flagged.sort();
      out.push(r);
    }
  }
  return out.sort(
    (a, b) =>
      a.bookId - b.bookId ||
      a.symbol.localeCompare(b.symbol) ||
      a.instrumentId - b.instrumentId,
  );
}
