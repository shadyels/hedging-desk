import { describe, expect, test } from "vitest";
import {
  ExecutionReport,
  InternalCrossNotice,
  OrdStatus as S,
} from "../gen/live";
import type { Entry } from "../store";
import {
  KINDS,
  NO_FILTER,
  buildBlotter,
  filterOptions,
  flatten,
  flattenFiltered,
  isFiltered,
  kindLabel,
  matchBook,
  matchInstrument,
  matchKind,
  matchStatus,
  matchText,
  rowMatches,
  type BlotterFilter,
  STATUS_LABELS,
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
    expect(statusGlyph(S.ORD_STATUS_PENDING_CANCEL)).toBe("◇");
    expect(statusGlyph(S.ORD_STATUS_REJECTED)).toBe("✕");
    expect(statusGlyph(S.ORD_STATUS_UNSPECIFIED)).toBe("");
  });

  test("N5: every OrdStatus has its own glyph and its own label (no two alike, none blank)", () => {
    const all = [
      S.ORD_STATUS_NEW,
      S.ORD_STATUS_PARTIALLY_FILLED,
      S.ORD_STATUS_FILLED,
      S.ORD_STATUS_CANCELED,
      S.ORD_STATUS_REJECTED,
      S.ORD_STATUS_PENDING_CANCEL,
      S.ORD_STATUS_PENDING_REPLACE,
    ];
    const glyphs = all.map(statusGlyph);
    expect(glyphs.every((g) => g !== "")).toBe(true);
    expect(new Set(glyphs).size).toBe(all.length);
    expect(new Set(all.map(statusLabel)).size).toBe(all.length);
    expect(STATUS_LABELS).toEqual(all.map(statusLabel));
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

// ---- N4: filters, all pure ---------------------------------------------------------------------------

/** Orders: cl-A (AAPL, FILLED, allocs to books 1 and 3), cl-B (MSFT, REJECTED, book 4 direct); crosses x-1 (AAPL, 1 buy 3 sell), x-2 (MSFT, 5 buy 6 sell). */
function fixture() {
  const alloc = (book: number) =>
    exec({
      clOrdId: "cl-A",
      execId: `ea${book}`,
      bookId: book,
      allocation: true,
      status: S.ORD_STATUS_FILLED,
      lastQtyE2: 50n,
      bookCumQtyE2: 50n,
      nettingCycleId: "run/1",
    });
  return buildBlotter(
    [
      exec({
        clOrdId: "cl-A",
        execId: "ea0",
        status: S.ORD_STATUS_FILLED,
        cumQtyE2: 100n,
        nettingCycleId: "run/1",
      }),
      alloc(1),
      alloc(3),
      exec({
        clOrdId: "cl-B",
        execId: "eb",
        bookId: 4,
        instrument: MSFT,
        status: S.ORD_STATUS_REJECTED,
      }),
    ],
    [
      cross({ crossId: "x-1" }),
      cross({
        crossId: "x-2",
        instrument: MSFT,
        buyBookId: 5,
        sellBookId: 6,
      }),
    ],
  );
}
const f = (p: Partial<BlotterFilter>): BlotterFilter => ({
  ...NO_FILTER,
  ...p,
});
const keys = (rows: ReturnType<typeof flatten>) =>
  rows.map((r) =>
    r.kind === "cross"
      ? r.crossId
      : r.kind === "alloc"
        ? `${r.clOrdId}/${r.bookId}`
        : r.clOrdId,
  );

describe("N4: filters are pure functions", () => {
  const m = fixture();
  const flat = flatten(m.items, new Set());
  const row = (k: string) => {
    const r = flat.find((x) => keys([x])[0] === k);
    if (!r) throw new Error(k);
    return r;
  };

  test("no filter is the plain flatten, and isFiltered sees every field", () => {
    expect(isFiltered(NO_FILTER)).toBe(false);
    expect(flattenFiltered(m.items, new Set(), NO_FILTER)).toEqual(flat);
    for (const k of Object.keys(NO_FILTER))
      expect(isFiltered(f({ [k]: "x" }))).toBe(true);
  });

  test("kind: ORDER, ALLOC and CROSS select their own rows only", () => {
    expect(KINDS).toEqual(["ORDER", "ALLOC", "CROSS"]);
    expect(flat.map(kindLabel).sort()).toEqual([
      "ALLOC",
      "ALLOC",
      "CROSS",
      "CROSS",
      "ORDER",
      "ORDER",
    ]);
    expect(matchKind(row("cl-A"), "ORDER")).toBe(true);
    expect(matchKind(row("cl-A"), "ALLOC")).toBe(false);
    expect(matchKind(row("cl-A/1"), "ALLOC")).toBe(true);
    expect(matchKind(row("x-1"), "CROSS")).toBe(true);
    expect(matchKind(row("x-1"), "")).toBe(true);
  });

  test("book: an order's own book, an allocation's book, and BOTH books of a cross", () => {
    expect(matchBook(row("cl-A"), "0")).toBe(true); // the firm netting parent
    expect(matchBook(row("cl-A"), "1")).toBe(false);
    expect(matchBook(row("cl-A/3"), "3")).toBe(true);
    expect(matchBook(row("cl-B"), "4")).toBe(true);
    expect(matchBook(row("x-1"), "1")).toBe(true); // buy side
    expect(matchBook(row("x-1"), "3")).toBe(true); // sell side
    expect(matchBook(row("x-1"), "5")).toBe(false);
    expect(matchBook(row("x-1"), "")).toBe(true);
  });

  test("instrument: by symbol, falling back to the id when there is none", () => {
    expect(matchInstrument(row("cl-A"), "AAPL")).toBe(true);
    expect(matchInstrument(row("cl-B"), "AAPL")).toBe(false);
    expect(matchInstrument(row("x-2"), "MSFT")).toBe(true);
    const nameless = buildBlotter(
      [exec({ instrument: { ...AAPL, symbol: "", instrumentId: 77 } })],
      [],
    ).items[0];
    expect(nameless && matchInstrument(nameless, "77")).toBe(true);
  });

  test("status: an order's own, an allocation inherits its parent's, a cross has none", () => {
    expect(matchStatus(S.ORD_STATUS_FILLED, "FILLED")).toBe(true);
    expect(matchStatus(S.ORD_STATUS_FILLED, "NEW")).toBe(false);
    expect(matchStatus(undefined, "FILLED")).toBe(false);
    expect(matchStatus(undefined, "")).toBe(true);
    expect(
      rowMatches(row("cl-A/1"), S.ORD_STATUS_FILLED, f({ status: "FILLED" })),
    ).toBe(true);
    expect(
      rowMatches(row("cl-A/1"), S.ORD_STATUS_NEW, f({ status: "FILLED" })),
    ).toBe(false);
    expect(rowMatches(row("x-1"), undefined, f({ status: "FILLED" }))).toBe(
      false,
    );
  });

  test("text: case-insensitive substring of the ClOrdID or CrossId, trimmed; blank matches all", () => {
    expect(matchText(row("cl-A"), "CL-a")).toBe(true);
    expect(matchText(row("cl-A/1"), "cl-")).toBe(true);
    expect(matchText(row("x-2"), " X-2 ")).toBe(true);
    expect(matchText(row("x-2"), "cl-")).toBe(false);
    expect(matchText(row("cl-B"), "")).toBe(true);
    expect(matchText(row("cl-B"), "   ")).toBe(true);
  });

  test("filters AND together over the flat list; a collapsed parent hides only allocations under a SHOWN parent", () => {
    expect(
      keys(
        flattenFiltered(m.items, new Set(), f({ instrument: "MSFT" })),
      ).sort(),
    ).toEqual(["cl-B", "x-2"]);
    expect(
      keys(
        flattenFiltered(
          m.items,
          new Set(),
          f({ instrument: "AAPL", kind: "CROSS" }),
        ),
      ),
    ).toEqual(["x-1"]);
    expect(
      keys(flattenFiltered(m.items, new Set(), f({ book: "3" }))).sort(),
    ).toEqual(["cl-A/3", "x-1"]);
    expect(
      keys(flattenFiltered(m.items, new Set(), f({ status: "REJECTED" }))),
    ).toEqual(["cl-B"]);
    expect(
      keys(
        flattenFiltered(
          m.items,
          new Set(),
          f({ status: "FILLED", kind: "ALLOC" }),
        ),
      ).sort(),
    ).toEqual(["cl-A/1", "cl-A/3"]);
    expect(flattenFiltered(m.items, new Set(), f({ text: "nope" }))).toEqual(
      [],
    );
    const parent = m.items.find(
      (i) => i.kind === "order" && i.clOrdId === "cl-A",
    );
    const collapsed = new Set([parent?.key ?? ""]);
    // parent shown (matches) and collapsed: its allocations hide
    expect(
      keys(
        flattenFiltered(
          m.items,
          collapsed,
          f({ instrument: "AAPL", kind: "" }),
        ),
      ),
    ).not.toContain("cl-A/1");
    // parent not shown (kind ALLOC): collapse must not strand the allocations
    expect(
      keys(flattenFiltered(m.items, collapsed, f({ kind: "ALLOC" }))),
    ).toEqual(expect.arrayContaining(["cl-A/1", "cl-A/3"]));
  });

  test("options come from the values present, sorted, and always keep the current choice", () => {
    const o = filterOptions(m.items);
    expect(o.books).toEqual([0, 1, 3, 4, 5, 6]);
    expect(o.instruments).toEqual(["AAPL", "MSFT"]);
    expect(o.statuses).toEqual(["FILLED", "REJECTED"]);
    expect(filterOptions([]).books).toEqual([]);
    const kept = filterOptions(
      [],
      f({ book: "9", instrument: "ZZZ", status: "NEW" }),
    );
    expect(kept).toEqual({
      books: [9],
      instruments: ["ZZZ"],
      statuses: ["NEW"],
    });
  });
});
