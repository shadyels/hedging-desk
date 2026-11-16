import { describe, expect, test } from "vitest";
import {
  ExecutionReport,
  InternalCrossNotice,
  OrdStatus as S,
} from "../gen/live";
import type { Entry } from "../store";
import {
  buildBlotter,
  flatten,
  statusGlyph,
  statusLabel,
  type CrossRow,
  type OrderRow,
} from "./model";

const AAPL = {
  instrumentId: 1,
  symbol: "AAPL",
  instrumentClass: 1,
  currency: "USD",
};
const MSFT = { ...AAPL, instrumentId: 2, symbol: "MSFT" };

let t = 1000;
const exec = (p: Partial<ExecutionReport>): Entry => ({
  value: ExecutionReport.fromPartial({
    clOrdId: "cl-1",
    instrument: AAPL,
    side: 1,
    ...p,
  }),
  receivedAtMs: t++,
});
const cross = (p: Partial<InternalCrossNotice>): Entry => ({
  value: InternalCrossNotice.fromPartial({
    crossId: "x-1",
    instrument: AAPL,
    buyBookId: 1,
    sellBookId: 3,
    qtyE2: 200_000n,
    ...p,
  }),
  receivedAtMs: t++,
});
const orders = (m: ReturnType<typeof buildBlotter>) =>
  m.items.filter((i): i is OrderRow => i.kind === "order");
const crosses = (m: ReturnType<typeof buildBlotter>) =>
  m.items.filter((i): i is CrossRow => i.kind === "cross");

describe("order build", () => {
  test("from a first fill: no New ack needed, qty = cum + leaves", () => {
    const m = buildBlotter(
      [
        exec({
          execId: "e1",
          status: S.ORD_STATUS_PARTIALLY_FILLED,
          lastQtyE2: 310_000n,
          lastPxE9: 241_200_000_000n,
          cumQtyE2: 310_000n,
          leavesQtyE2: 220_000n,
        }),
      ],
      [],
    );
    const [o] = orders(m);
    expect(o?.qtyE2).toBe(530_000n);
    expect(o?.status).toBe(S.ORD_STATUS_PARTIALLY_FILLED);
    expect(o?.lastPxE9).toBe(241_200_000_000n);
  });

  test("from a first reject: a row exists, no price, reason kept", () => {
    const m = buildBlotter(
      [
        exec({
          execId: "e1",
          status: S.ORD_STATUS_REJECTED,
          leavesQtyE2: 90_000n,
          text: "risk: limit",
        }),
      ],
      [],
    );
    const [o] = orders(m);
    expect(o?.status).toBe(S.ORD_STATUS_REJECTED);
    expect(o?.lastPxE9).toBe(0n);
    expect(o?.text).toBe("risk: limit");
    expect(statusGlyph(S.ORD_STATUS_REJECTED)).toBe("✕");
  });

  test("a New ack alone is an order too, and keys are (instrument, cl_ord_id)", () => {
    const m = buildBlotter(
      [
        exec({ execId: "e1", status: S.ORD_STATUS_NEW, leavesQtyE2: 100n }),
        exec({
          execId: "e2",
          instrument: MSFT,
          status: S.ORD_STATUS_NEW,
          leavesQtyE2: 100n,
        }),
      ],
      [],
    );
    expect(orders(m)).toHaveLength(2); // same cl_ord_id, two instruments
  });

  test("status follows cl_ord_id across reports; a late New ack does not regress it", () => {
    const base = { clOrdId: "cl-7" };
    const m = buildBlotter(
      [
        exec({
          ...base,
          execId: "e1",
          status: S.ORD_STATUS_PARTIALLY_FILLED,
          cumQtyE2: 100n,
          leavesQtyE2: 300n,
        }),
        exec({
          ...base,
          execId: "e2",
          status: S.ORD_STATUS_NEW,
          cumQtyE2: 0n,
          leavesQtyE2: 400n,
        }),
        exec({
          ...base,
          execId: "e3",
          status: S.ORD_STATUS_FILLED,
          cumQtyE2: 400n,
          leavesQtyE2: 0n,
        }),
        exec({ ...base, clOrdId: "cl-8", execId: "e4", leavesQtyE2: 5n }),
      ],
      [],
    );
    expect(orders(m)).toHaveLength(2);
    const o = orders(m).find((x) => x.clOrdId === "cl-7");
    expect(o?.status).toBe(S.ORD_STATUS_FILLED);
    expect(o?.cumQtyE2).toBe(400n);
    expect(o?.qtyE2).toBe(400n); // from the first report: 100 + 300
  });

  test("status labels and shape cues are neutral text", () => {
    expect(statusLabel(S.ORD_STATUS_PENDING_CANCEL)).toBe("PENDING_CANCEL");
    expect(statusGlyph(S.ORD_STATUS_PENDING_REPLACE)).toBe("◇");
    expect(statusGlyph(S.ORD_STATUS_FILLED)).toBe("");
  });
});

describe("dedupe on (exec_id, book_id)", () => {
  test("a duplicate is ignored and counted; the same exec_id on another book is kept", () => {
    const fill = {
      execId: "E1",
      status: S.ORD_STATUS_PARTIALLY_FILLED,
      cumQtyE2: 100n,
      leavesQtyE2: 100n,
    };
    const alloc = (book: number) =>
      exec({
        ...fill,
        bookId: book,
        allocation: true,
        lastQtyE2: 50n,
        bookCumQtyE2: 50n,
        nettingCycleId: "run/1",
      });
    const m = buildBlotter(
      [exec(fill), exec(fill), alloc(1), alloc(3), alloc(1)],
      [],
    );
    expect(m.duplicates).toBe(2);
    const [o] = orders(m);
    expect(o?.allocs.map((a) => a.bookId)).toEqual([1, 3]);
  });

  test("an empty exec_id is not deduped", () => {
    const m = buildBlotter(
      [exec({ clOrdId: "a", leavesQtyE2: 1n }), exec({ clOrdId: "b" })],
      [],
    );
    expect(orders(m)).toHaveLength(2);
    expect(m.duplicates).toBe(0);
  });
});

describe("allocation children", () => {
  const cycle = "run/1";
  const parent = exec({
    execId: "E1",
    status: S.ORD_STATUS_PARTIALLY_FILLED,
    cumQtyE2: 300_000n,
    leavesQtyE2: 200_000n,
    nettingCycleId: cycle,
  });
  const alloc = exec({
    execId: "E1",
    bookId: 3,
    allocation: true,
    status: S.ORD_STATUS_PARTIALLY_FILLED,
    cumQtyE2: 300_000n, // the PARENT's cum
    leavesQtyE2: 200_000n,
    lastQtyE2: 110_000n,
    bookCumQtyE2: 110_000n,
    nettingCycleId: cycle,
  });

  test("the flag nests a row under its parent; the book share is not the parent's cum", () => {
    const m = buildBlotter([parent, alloc], []);
    const [o] = orders(m);
    expect(m.items).toHaveLength(1);
    expect(o?.cumQtyE2).toBe(300_000n);
    const [a] = o?.allocs ?? [];
    expect(a?.lastQtyE2).toBe(110_000n);
    expect(a?.bookCumQtyE2).toBe(110_000n);
    expect(a?.bookCumQtyE2).not.toBe(o?.cumQtyE2);
    expect(flatten(m.items, new Set()).map((r) => r.kind)).toEqual([
      "order",
      "alloc",
    ]);
    expect(flatten(m.items, new Set([o?.key ?? ""]))).toHaveLength(1);
  });

  test("an allocation arriving first builds the order; a later allocation never overrides the parent report", () => {
    const m = buildBlotter(
      [
        alloc,
        exec({
          execId: "E2",
          status: S.ORD_STATUS_FILLED,
          cumQtyE2: 500_000n,
          nettingCycleId: cycle,
        }),
        // a straggling allocation of E1 repeats E1's parent state
        exec({ ...alloc.value, execId: "E1b", bookId: 1 } as ExecutionReport),
      ],
      [],
    );
    const [o] = orders(m);
    expect(o?.bookId).toBe(0);
    expect(o?.status).toBe(S.ORD_STATUS_FILLED);
    expect(o?.cumQtyE2).toBe(500_000n);
    expect(o?.allocs).toHaveLength(2);
  });

  test("a direct order has no allocation children", () => {
    const m = buildBlotter(
      [
        exec({
          execId: "D1",
          bookId: 5,
          status: S.ORD_STATUS_FILLED,
          cumQtyE2: 100n,
          nettingCycleId: "",
        }),
      ],
      [],
    );
    const [o] = orders(m);
    expect(o?.bookId).toBe(5);
    expect(o?.allocs).toEqual([]);
    expect(flatten(m.items, new Set())).toHaveLength(1);
  });
});

describe("cross lineage by netting_cycle_id", () => {
  const parent = (instrument = AAPL) =>
    exec({
      execId: `E-${instrument.symbol}`,
      instrument,
      clOrdId: `cl-${instrument.symbol}`,
      status: S.ORD_STATUS_NEW,
      leavesQtyE2: 100n,
      nettingCycleId: "run/9",
    });

  test("links to its residual parent order of the same cycle and instrument", () => {
    const m = buildBlotter(
      [parent(MSFT), parent(AAPL)],
      [cross({ nettingCycleId: "run/9" })],
    );
    const [x] = crosses(m);
    expect(x?.link).toEqual({
      kind: "order",
      orderKey: "o:1|cl-AAPL",
      clOrdId: "cl-AAPL",
    });
  });

  test("DIRECT is a transfer, empty is no link, an unseen parent is no residual", () => {
    const m = buildBlotter(
      [parent()],
      [
        cross({ crossId: "t", nettingCycleId: "DIRECT" }),
        cross({ crossId: "e", nettingCycleId: "" }),
        cross({ crossId: "n", nettingCycleId: "run/10" }),
        cross({ crossId: "i", nettingCycleId: "run/9", instrument: MSFT }),
      ],
    );
    const by = Object.fromEntries(crosses(m).map((c) => [c.crossId, c.link]));
    expect(by["t"]).toEqual({ kind: "transfer" });
    expect(by["e"]).toEqual({ kind: "none" });
    expect(by["n"]).toEqual({ kind: "no-residual" });
    expect(by["i"]).toEqual({ kind: "no-residual" }); // same cycle, other instrument's order
  });

  test("a repeated cross_id is one row; the list is newest first", () => {
    const m = buildBlotter(
      [parent()],
      [cross({ crossId: "x" }), cross({ crossId: "x" })],
    );
    expect(crosses(m)).toHaveLength(1);
    expect(m.duplicates).toBe(1);
    expect(m.items[0]?.kind).toBe("cross"); // arrived last
  });
});
