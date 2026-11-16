/**
 * Pure blotter logic (P3.M1 Slice 5): folds the d1.exec and d1.crosses rings into rows. No React, no clock.
 * Quantities and prices stay bigint end to end; display goes through format.ts.
 *
 * Orders are keyed by (instrument_id, cl_ord_id). The exec stream has no snapshot and a NEW ack is not guaranteed
 * to precede a fill (ADR-014 Amend 1 s3), so an order is built from whatever report arrives FIRST.
 * Reports dedupe on (exec_id, book_id) (ADR-014 s5), not on arrival count.
 */
import type { WithMeta } from "../bus/subjects";
import { Side, type InstrumentRef } from "../gen/common";
import {
  OrdStatus,
  type ExecutionReport,
  type InternalCrossNotice,
} from "../gen/live";
import { MISSING } from "../format";
import type { Entry } from "../store";

export const asExec = (v: WithMeta | undefined): ExecutionReport | null =>
  v &&
  "clOrdId" in v &&
  "execId" in v &&
  "allocation" in v &&
  "lastQtyE2" in v &&
  typeof v.lastQtyE2 === "bigint"
    ? (v as ExecutionReport)
    : null;

export const asCross = (v: WithMeta | undefined): InternalCrossNotice | null =>
  v &&
  "crossId" in v &&
  "buyBookId" in v &&
  "qtyE2" in v &&
  typeof v.qtyE2 === "bigint"
    ? (v as InternalCrossNotice)
    : null;

/** netting_cycle_id of a directed transfer (ADR-009, ADR-014 Amend 1 s4). There is no parent order. */
export const DIRECT = "DIRECT";

export interface AllocRow {
  kind: "alloc";
  key: string;
  parentKey: string;
  atMs: number;
  clOrdId: string;
  execId: string;
  bookId: number;
  instrument: InstrumentRef | undefined;
  side: Side;
  /** This book's share of the venue execution (last_qty_e2 on an allocation report). */
  lastQtyE2: bigint;
  lastPxE9: bigint;
  /** Cumulative quantity allocated to THIS book (book_cum_qty_e2). Never the parent's cum. */
  bookCumQtyE2: bigint;
}

export interface OrderRow {
  kind: "order";
  key: string;
  clOrdId: string;
  instrument: InstrumentRef | undefined;
  /** 0 = firm-level netting parent; != 0 = a direct order on its own book. */
  bookId: number;
  side: Side;
  status: OrdStatus;
  /** cum + leaves on the FIRST report seen. */
  qtyE2: bigint;
  cumQtyE2: bigint;
  leavesQtyE2: bigint;
  /** Latest non-zero fill price; 0n = none yet. */
  lastPxE9: bigint;
  /** Latest non-empty reject reason / free text. */
  text: string;
  /** "" = direct order, no allocation children. */
  nettingCycleId: string;
  atMs: number;
  seq: number;
  allocs: AllocRow[];
}

export type CrossLink =
  | { kind: "transfer" }
  | { kind: "none" }
  | { kind: "no-residual" }
  | { kind: "order"; orderKey: string; clOrdId: string };

export interface CrossRow {
  kind: "cross";
  key: string;
  crossId: string;
  instrument: InstrumentRef | undefined;
  buyBookId: number;
  sellBookId: number;
  qtyE2: bigint;
  refPxE9: bigint;
  pxPolicyId: string;
  nettingCycleId: string;
  atMs: number;
  seq: number;
  link: CrossLink;
}

export type TopRow = OrderRow | CrossRow;
export type FlatRow = TopRow | AllocRow;

export interface BlotterModel {
  /** Newest first. Top-level rows only: orders and crosses. */
  items: TopRow[];
  /** Reports and crosses ignored as (exec_id, book_id) / cross_id repeats, within the ring window. */
  duplicates: number;
}

const iid = (i: InstrumentRef | undefined) => i?.instrumentId ?? 0;

export function buildBlotter(
  exec: readonly Entry[] | undefined,
  crosses: readonly Entry[] | undefined,
): BlotterModel {
  const orders = new Map<string, OrderRow>();
  const seen = new Set<string>();
  const hasParent = new Set<string>();
  let duplicates = 0;
  let seq = 0;

  for (const entry of exec ?? []) {
    const r = asExec(entry.value);
    if (!r) continue;
    // ponytail: a report with an empty exec_id cannot be keyed, so it is never deduped here (the store's
    // msg_id LRU still catches transport repeats). Deduping on ("", book) would drop distinct orders.
    if (r.execId !== "") {
      const dk = `${r.execId}\u0000${r.bookId}`;
      if (seen.has(dk)) {
        duplicates += 1;
        continue;
      }
      seen.add(dk);
    }
    const key = `o:${iid(r.instrument)}|${r.clOrdId}`;
    let o = orders.get(key);
    if (!o) {
      o = {
        kind: "order",
        key,
        clOrdId: r.clOrdId,
        instrument: r.instrument,
        bookId: r.allocation ? 0 : r.bookId,
        side: r.side,
        status: r.status,
        qtyE2: r.cumQtyE2 + r.leavesQtyE2,
        cumQtyE2: r.cumQtyE2,
        leavesQtyE2: r.leavesQtyE2,
        lastPxE9: r.lastPxE9 > 0n ? r.lastPxE9 : 0n,
        text: r.text,
        nettingCycleId: r.nettingCycleId,
        atMs: entry.receivedAtMs,
        seq: seq++,
        allocs: [],
      };
      orders.set(key, o);
    } else {
      if (!o.instrument) o.instrument = r.instrument;
      if (o.nettingCycleId === "") o.nettingCycleId = r.nettingCycleId;
      // The parent's status/cum/leaves come from the parent's own report. An allocation report repeats them, so it
      // only updates state while no parent report has been seen (first-report build) and never after.
      // A report whose cum is below what is already shown is a late ack: cum only grows, so it cannot be newer.
      if (!r.allocation) o.bookId = r.bookId;
      const mayUpdate = !r.allocation || !hasParent.has(key);
      if (mayUpdate && r.cumQtyE2 >= o.cumQtyE2) {
        o.status = r.status;
        o.cumQtyE2 = r.cumQtyE2;
        o.leavesQtyE2 = r.leavesQtyE2;
        if (r.lastPxE9 > 0n) o.lastPxE9 = r.lastPxE9;
        if (r.text !== "") o.text = r.text;
      }
    }
    if (!r.allocation) hasParent.add(key);
    if (r.allocation) {
      o.allocs.push({
        kind: "alloc",
        key: `a:${key}|${r.execId}|${r.bookId}|${o.allocs.length}`,
        parentKey: key,
        atMs: entry.receivedAtMs,
        clOrdId: r.clOrdId,
        execId: r.execId,
        bookId: r.bookId,
        instrument: r.instrument,
        side: r.side,
        lastQtyE2: r.lastQtyE2,
        lastPxE9: r.lastPxE9,
        bookCumQtyE2: r.bookCumQtyE2,
      });
    }
  }

  // Lineage: a cross and its residual parent share netting_cycle_id (ADR-014 Amend 1 s4). One cycle nets many
  // instruments, so the parent is the order of that cycle on the cross's own instrument. Never matched by time.
  const byCycle = new Map<string, OrderRow>();
  for (const o of orders.values()) {
    const k = `${o.nettingCycleId}\u0000${iid(o.instrument)}`;
    if (o.nettingCycleId !== "" && !byCycle.has(k)) byCycle.set(k, o);
  }

  const items: TopRow[] = [...orders.values()];
  const seenCross = new Set<string>();
  for (const entry of crosses ?? []) {
    const c = asCross(entry.value);
    if (!c) continue;
    if (c.crossId !== "") {
      if (seenCross.has(c.crossId)) {
        duplicates += 1;
        continue;
      }
      seenCross.add(c.crossId);
    }
    const parent =
      c.nettingCycleId === "" || c.nettingCycleId === DIRECT
        ? undefined
        : byCycle.get(`${c.nettingCycleId}\u0000${iid(c.instrument)}`);
    const link: CrossLink =
      c.nettingCycleId === DIRECT
        ? { kind: "transfer" }
        : c.nettingCycleId === ""
          ? { kind: "none" }
          : parent
            ? { kind: "order", orderKey: parent.key, clOrdId: parent.clOrdId }
            : { kind: "no-residual" };
    items.push({
      kind: "cross",
      key: `x:${c.crossId || `seq${seq}`}`,
      crossId: c.crossId,
      instrument: c.instrument,
      buyBookId: c.buyBookId,
      sellBookId: c.sellBookId,
      qtyE2: c.qtyE2,
      refPxE9: c.refPxE9,
      pxPolicyId: c.pxPolicyId,
      nettingCycleId: c.nettingCycleId,
      atMs: entry.receivedAtMs,
      seq: seq++,
      link,
    });
  }

  items.sort((a, b) => b.atMs - a.atMs || b.seq - a.seq);
  return { items, duplicates };
}

/** Display order: each order followed by its allocation children, unless collapsed. */
export function flatten(
  items: readonly TopRow[],
  collapsed: ReadonlySet<string>,
): FlatRow[] {
  const out: FlatRow[] = [];
  for (const it of items) {
    out.push(it);
    if (it.kind === "order" && !collapsed.has(it.key)) out.push(...it.allocs);
  }
  return out;
}

const STATUS: Partial<Record<OrdStatus, string>> = {
  [OrdStatus.ORD_STATUS_NEW]: "NEW",
  [OrdStatus.ORD_STATUS_PARTIALLY_FILLED]: "PARTIALLY_FILLED",
  [OrdStatus.ORD_STATUS_FILLED]: "FILLED",
  [OrdStatus.ORD_STATUS_CANCELED]: "CANCELED",
  [OrdStatus.ORD_STATUS_REJECTED]: "REJECTED",
  [OrdStatus.ORD_STATUS_PENDING_CANCEL]: "PENDING_CANCEL",
  [OrdStatus.ORD_STATUS_PENDING_REPLACE]: "PENDING_REPLACE",
};

export const statusLabel = (s: OrdStatus): string =>
  STATUS[s] ?? (s === OrdStatus.ORD_STATUS_UNSPECIFIED ? MISSING : "UNKNOWN");

/** Shape cue only, never a hue (DESIGN.md "Blotter row"). */
export const statusGlyph = (s: OrdStatus): string =>
  s === OrdStatus.ORD_STATUS_REJECTED
    ? "✕"
    : s === OrdStatus.ORD_STATUS_PENDING_CANCEL ||
        s === OrdStatus.ORD_STATUS_PENDING_REPLACE
      ? "◇"
      : "";

export const sideLabel = (s: Side): string =>
  s === Side.SIDE_BUY ? "BUY" : s === Side.SIDE_SELL ? "SELL" : MISSING;
