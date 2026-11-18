import {
  act,
  cleanup,
  fireEvent,
  getByLabelText,
  render,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { freshness } from "../bus/freshness";
import { SUBJECTS, keyOf } from "../bus/subjects";
import {
  ExecutionReport,
  InternalCrossNotice,
  OrdStatus as S,
} from "../gen/live";
import { busSink, flushBus, initialState, resetBus } from "../store";
import { useNow } from "../useNow";
import blotterCss from "../styles/blotter.css?raw";
import { BlotterBoard, DEFAULT_ROW_H as ROW_H } from "./BlotterBoard";

let seq = 0;
const T0 = Date.UTC(2026, 10, 8, 12, 4, 9);
/** jsdom has no layout: give the scroller the 20-row viewport the stylesheet gives it. */
const clientHeight = Object.getOwnPropertyDescriptor(
  Element.prototype,
  "clientHeight",
);
beforeEach(() => {
  Object.defineProperty(Element.prototype, "clientHeight", {
    configurable: true,
    get(this: Element) {
      return this.classList.contains("bl-scroll") ? ROW_H * 20 : 0;
    },
  });
  vi.useFakeTimers();
  vi.setSystemTime(T0);
  resetBus();
});
afterEach(() => {
  if (clientHeight)
    Object.defineProperty(Element.prototype, "clientHeight", clientHeight);
  cleanup();
  vi.useRealTimers();
});

function Host() {
  return <BlotterBoard now={useNow()} />;
}
const AAPL = {
  instrumentId: 1001, // the wire carries the id only; the symbol comes from refdata
  symbol: "",
  instrumentClass: 1,
  currency: "USD",
};
const up = () =>
  act(() =>
    busSink.onStatus(
      "connected",
      { attempt: 0, nextRetryAtMs: null },
      Date.now(),
    ),
  );
const tick = (ms: number) => act(() => void vi.advanceTimersByTime(ms));

const sendExec = (...ps: Partial<ExecutionReport>[]) =>
  act(() => {
    for (const p of ps) {
      const book = p.bookId ?? 0;
      const subject = `d1.exec.${book}.1`;
      busSink.onMessage(
        "exec",
        subject,
        keyOf(SUBJECTS.exec.pattern, subject),
        ExecutionReport.fromPartial({
          meta: { msgId: `m${seq++}` },
          clOrdId: `cl-${seq}`,
          execId: `e${seq}`,
          instrument: AAPL,
          side: 1,
          status: S.ORD_STATUS_NEW,
          leavesQtyE2: 100_000n,
          ...p,
        }),
        Date.now(),
      );
    }
    flushBus();
  });
const sendCross = (p: Partial<InternalCrossNotice>) =>
  act(() => {
    busSink.onMessage(
      "crosses",
      "d1.crosses",
      "",
      InternalCrossNotice.fromPartial({
        meta: { msgId: `m${seq++}` },
        crossId: `x-${seq}`,
        instrument: AAPL,
        buyBookId: 1,
        sellBookId: 3,
        qtyE2: 200_000n,
        ...p,
      }),
      Date.now(),
    );
    flushBus();
  });

const scroller = (c: HTMLElement) => {
  const el = c.querySelector<HTMLElement>(".bl-scroll");
  if (!el) throw new Error("no scroller");
  return el;
};
const rowEls = (c: HTMLElement) =>
  Array.from(scroller(c).querySelectorAll<HTMLElement>('[role="row"]'));
/** The row whose top edge is at the scroll offset: the first visible row. */
const firstVisible = (c: HTMLElement) => {
  const top = scroller(c).scrollTop;
  const el = rowEls(c).find(
    (r) => r.style.transform === `translateY(${top}px)`,
  );
  return el?.dataset["key"];
};

describe("virtualization", () => {
  test("a 1,000-row fixture renders a bounded number of DOM rows", () => {
    const { container } = render(<Host />);
    up();
    sendExec(
      ...Array.from({ length: 1000 }, (_, i) => ({
        clOrdId: `cl-${i}`,
        execId: `v${i}`,
      })),
    );
    const n = rowEls(container).length;
    expect(n).toBeGreaterThan(10);
    expect(n).toBeLessThan(60);
    const table = container.querySelector('[role="table"]');
    expect(table?.getAttribute("aria-rowcount")).toBe("1001");
  });

  test("the row height is read from the --row-h token", () => {
    document.documentElement.style.setProperty("--row-h", "30px");
    try {
      const { container } = render(<Host />);
      up();
      sendExec({ clOrdId: "a" }, { clOrdId: "b" });
      const ys = rowEls(container).map((r) => r.style.transform);
      expect(ys).toEqual(["translateY(0px)", "translateY(30px)"]);
    } finally {
      document.documentElement.style.removeProperty("--row-h");
    }
  });
});

describe("pause-scroll", () => {
  test("new rows arriving while scrolled do not move the first visible row; resume jumps back", () => {
    const { container, getByRole, queryByRole } = render(<Host />);
    up();
    sendExec(
      ...Array.from({ length: 80 }, (_, i) => ({
        clOrdId: `cl-${i}`,
        execId: `p${i}`,
      })),
    );
    const el = scroller(container);
    el.scrollTop = ROW_H * 12;
    fireEvent.scroll(el);
    const before = firstVisible(container);
    expect(before).toBeDefined();
    expect(queryByRole("button", { name: /resume/ })).toBeNull();

    sendExec(
      ...Array.from({ length: 5 }, (_, i) => ({
        clOrdId: `new-${i}`,
        execId: `n${i}`,
      })),
    );
    expect(el.scrollTop).toBe(ROW_H * 17); // shifted by exactly the 5 new rows
    expect(firstVisible(container)).toBe(before);
    const resume = getByRole("button", { name: "5 new, resume" });

    fireEvent.click(resume);
    expect(el.scrollTop).toBe(0);
    expect(queryByRole("button", { name: /resume/ })).toBeNull();
  });

  test("at the top the list stays live: new rows show first and no control appears", () => {
    const { container, queryByRole } = render(<Host />);
    up();
    sendExec({ clOrdId: "cl-old", execId: "a" });
    sendExec({ clOrdId: "cl-new", execId: "b" });
    expect(rowEls(container)[0]?.textContent).toContain("cl-new");
    expect(queryByRole("button", { name: /resume/ })).toBeNull();
  });
});

describe("arrow-key row selection", () => {
  const key = (c: HTMLElement, k: string) =>
    fireEvent.keyDown(scroller(c), { key: k });
  const selected = (c: HTMLElement) =>
    rowEls(c).filter((r) => r.getAttribute("aria-selected") === "true");
  /** The id of the selected row's ClOrdID cell text, or null. */
  const selText = (c: HTMLElement) => selected(c)[0]?.textContent ?? null;
  /** Oldest to newest: leaf cl-k-a, then parent cl-k-p with two allocations, then leaf cl-k-z. Shown newest first. */
  const fixture = () => {
    sendExec({ clOrdId: "cl-k-a", execId: "K1" });
    sendExec(
      { clOrdId: "cl-k-p", execId: "K2", nettingCycleId: "run/k" },
      {
        clOrdId: "cl-k-p",
        execId: "K2",
        bookId: 3,
        allocation: true,
        nettingCycleId: "run/k",
        lastQtyE2: 1n,
        bookCumQtyE2: 1n,
      },
      {
        clOrdId: "cl-k-p",
        execId: "K2",
        bookId: 4,
        allocation: true,
        nettingCycleId: "run/k",
        lastQtyE2: 1n,
        bookCumQtyE2: 1n,
      },
    );
    sendExec({ clOrdId: "cl-k-z", execId: "K3" });
  };
  const kinds = (c: HTMLElement) => rowEls(c).map((r) => r.dataset["kind"]);

  test("nothing is selected until a key is pressed; Down and Up walk every visible row, child rows included", () => {
    const { container } = render(<Host />);
    up();
    fixture();
    expect(kinds(container)).toEqual([
      "order",
      "order",
      "alloc",
      "alloc",
      "order",
    ]);
    expect(selected(container)).toHaveLength(0);
    expect(scroller(container).hasAttribute("aria-activedescendant")).toBe(
      false,
    );
    const seen: (string | undefined)[] = [];
    for (let i = 0; i < 5; i++) {
      key(container, "ArrowDown");
      expect(selected(container)).toHaveLength(1);
      seen.push(selected(container)[0]?.dataset["kind"]);
    }
    expect(seen).toEqual(["order", "order", "alloc", "alloc", "order"]);
    key(container, "ArrowDown"); // clamps at the last row
    expect(selText(container)).toContain("cl-k-a");
    key(container, "ArrowUp");
    expect(selected(container)[0]?.dataset["kind"]).toBe("alloc");
    key(container, "ArrowUp");
    key(container, "ArrowUp");
    expect(selText(container)).toContain("cl-k-p");
    key(container, "ArrowUp");
    key(container, "ArrowUp"); // clamps at the first row
    expect(selText(container)).toContain("cl-k-z");
  });

  test("Home and End go to the first and last row", () => {
    const { container } = render(<Host />);
    up();
    fixture();
    key(container, "End");
    expect(selText(container)).toContain("cl-k-a");
    key(container, "Home");
    expect(selText(container)).toContain("cl-k-z");
  });

  test("the selected row is marked: aria-selected, the class, and the scroller's aria-activedescendant", () => {
    const { container } = render(<Host />);
    up();
    fixture();
    key(container, "ArrowDown");
    const [row] = selected(container);
    expect(row?.classList.contains("is-selected")).toBe(true);
    expect(row?.id).not.toBe("");
    expect(scroller(container).getAttribute("aria-activedescendant")).toBe(
      row?.id,
    );
    expect(
      rowEls(container).filter(
        (r) => r.getAttribute("aria-selected") === "false",
      ),
    ).toHaveLength(4);
    expect(scroller(container).tabIndex).toBe(0);
  });

  test("Enter and Space toggle a parent; on a leaf they do nothing", () => {
    const { container } = render(<Host />);
    up();
    fixture();
    key(container, "Enter"); // nothing selected yet
    expect(kinds(container)).toHaveLength(5);
    key(container, "ArrowDown"); // leaf cl-k-z
    key(container, "Enter");
    key(container, " ");
    expect(kinds(container)).toHaveLength(5);
    key(container, "ArrowDown"); // parent cl-k-p
    key(container, "Enter");
    expect(kinds(container)).toEqual(["order", "order", "order"]);
    expect(selText(container)).toContain("cl-k-p"); // still selected, now collapsed
    key(container, " ");
    expect(kinds(container)).toHaveLength(5);
  });

  test("the selection is a key: a new row arriving does not move it", () => {
    const { container } = render(<Host />);
    up();
    fixture();
    key(container, "ArrowDown");
    key(container, "ArrowDown");
    expect(selText(container)).toContain("cl-k-p");
    sendExec({ clOrdId: "cl-k-new", execId: "K4" });
    expect(rowEls(container)[0]?.textContent).toContain("cl-k-new");
    expect(selected(container)).toHaveLength(1);
    expect(selText(container)).toContain("cl-k-p");
    key(container, "ArrowDown");
    expect(selected(container)[0]?.dataset["kind"]).toBe("alloc");
  });

  test("a selected row removed by a filter clears the selection, it does not jump", () => {
    const { container } = render(<Host />);
    up();
    fixture();
    sendCross({ crossId: "x-k", nettingCycleId: "" });
    key(container, "ArrowDown"); // the cross, newest
    key(container, "ArrowDown"); // cl-k-z
    expect(selText(container)).toContain("cl-k-z");
    fireEvent.change(getByLabelText(container, "Kind"), {
      target: { value: "CROSS" },
    });
    expect(rowEls(container)).toHaveLength(1);
    expect(selected(container)).toHaveLength(0);
    expect(scroller(container).hasAttribute("aria-activedescendant")).toBe(
      false,
    );
    fireEvent.change(getByLabelText(container, "Kind"), {
      target: { value: "" },
    });
    expect(selected(container)).toHaveLength(0); // does not come back on its own
  });

  test("moving past the viewport scrolls the row into view and pauses the live tail", () => {
    const { container, getByRole, queryByRole } = render(<Host />);
    up();
    sendExec(
      ...Array.from({ length: 80 }, (_, i) => ({
        clOrdId: `cl-kb-${i}`,
        execId: `kb${i}`,
      })),
    );
    const el = scroller(container);
    for (let i = 0; i < 20; i++) key(container, "ArrowDown"); // row 19: last one in the 20-row viewport
    expect(el.scrollTop).toBe(0);
    expect(queryByRole("button", { name: /resume/ })).toBeNull();
    key(container, "ArrowDown"); // row 20: just outside
    expect(el.scrollTop).toBe(ROW_H);
    key(container, "End");
    expect(el.scrollTop).toBe(ROW_H * 60);
    fireEvent.scroll(el); // a browser fires this after the programmatic scroll; jsdom does not
    expect(selText(container)).toContain("cl-kb-0"); // the oldest, last row, is rendered and selected
    const before = firstVisible(container);
    sendExec(
      ...Array.from({ length: 3 }, (_, i) => ({
        clOrdId: `cl-kn-${i}`,
        execId: `kn${i}`,
      })),
    );
    expect(firstVisible(container)).toBe(before);
    expect(selText(container)).toContain("cl-kb-0");
    fireEvent.click(getByRole("button", { name: "3 new, resume" }));
    expect(el.scrollTop).toBe(0);
    key(container, "Home");
    expect(el.scrollTop).toBe(0);
    expect(queryByRole("button", { name: /resume/ })).toBeNull();
  });
});

describe("N4: filter row", () => {
  const MSFT = { ...AAPL, instrumentId: 1002 };
  const pick = (c: HTMLElement, label: string, value: string) =>
    fireEvent.change(getByLabelText(c, label), { target: { value } });
  const ids = (c: HTMLElement) =>
    rowEls(c).map(
      (r) => r.querySelector(".c-id")?.textContent?.replace(" (net)", "") ?? "",
    );
  const opts = (c: HTMLElement, label: string) =>
    Array.from((getByLabelText(c, label) as HTMLSelectElement).options).map(
      (o) => o.value,
    );

  test("the controls mount before any data arrives, and are labelled", () => {
    const { container, getByRole } = render(<Host />);
    up();
    expect(getByRole("group", { name: "Blotter filters" })).toBeDefined();
    for (const l of ["Kind", "Book", "Instrument", "Status", "ClOrdID"])
      expect(getByLabelText(container, l)).toBeDefined();
    expect(opts(container, "Kind")).toEqual(["", "ORDER", "ALLOC", "CROSS"]);
    expect(opts(container, "Book")).toEqual([""]);
  });

  test("options are built from the ring; each filter narrows the rows; Clear restores them", () => {
    const { container, getByRole, queryByRole } = render(<Host />);
    up();
    sendExec(
      { clOrdId: "cl-aapl", execId: "f1", status: S.ORD_STATUS_FILLED },
      {
        clOrdId: "cl-msft",
        execId: "f2",
        instrument: MSFT,
        bookId: 4,
        status: S.ORD_STATUS_REJECTED,
      },
    );
    sendCross({ crossId: "x-77", buyBookId: 8, sellBookId: 9 });
    expect(opts(container, "Instrument")).toEqual(["", "AAPL", "MSFT"]);
    expect(opts(container, "Book")).toEqual(["", "0", "4", "8", "9"]);
    expect(opts(container, "Status")).toEqual(["", "FILLED", "REJECTED"]);
    expect(rowEls(container)).toHaveLength(3);

    pick(container, "Instrument", "MSFT");
    expect(ids(container)).toEqual(["cl-msft"]);
    pick(container, "Instrument", "");
    pick(container, "Status", "FILLED");
    expect(ids(container)).toEqual(["cl-aapl"]);
    pick(container, "Status", "");
    pick(container, "Kind", "CROSS");
    expect(ids(container)).toEqual(["x-77"]);
    pick(container, "Kind", "");
    pick(container, "Book", "9"); // the sell side of the cross
    expect(ids(container)).toEqual(["x-77"]);
    pick(container, "Book", "");
    fireEvent.change(getByLabelText(container, "ClOrdID"), {
      target: { value: "CL-MS" },
    });
    expect(ids(container)).toEqual(["cl-msft"]);
    expect(container.querySelector(".bl-count")?.textContent).toBe(
      "1 row match",
    );

    fireEvent.change(getByLabelText(container, "ClOrdID"), {
      target: { value: "no such id" },
    });
    expect(container.querySelector(".bl-scroll")).toBeNull();
    expect(container.textContent).toContain("No row matches the filters.");
    expect(container.textContent).not.toContain("none seen yet");
    fireEvent.click(getByRole("button", { name: "Clear" }));
    expect(rowEls(container)).toHaveLength(3);
    expect(queryByRole("button", { name: "Clear" })).toBeNull();
  });

  test("the filter row does not break pause-scroll: the held row stays put while matching rows arrive", () => {
    const { container, getByRole } = render(<Host />);
    up();
    sendExec(
      ...Array.from({ length: 80 }, (_, i) => ({
        clOrdId: `cl-${i}`,
        execId: `q${i}`,
      })),
      { clOrdId: "other", execId: "q-other", instrument: MSFT },
    );
    pick(container, "Instrument", "AAPL");
    const el = scroller(container);
    expect(el.scrollTop).toBe(0);
    el.scrollTop = ROW_H * 12;
    fireEvent.scroll(el);
    const before = firstVisible(container);
    expect(before).toBeDefined();

    sendExec(
      ...Array.from({ length: 5 }, (_, i) => ({
        clOrdId: `new-${i}`,
        execId: `qn${i}`,
      })),
    );
    expect(el.scrollTop).toBe(ROW_H * 17);
    expect(firstVisible(container)).toBe(before);
    // a row the filter hides does not count as new and does not move the viewport
    sendExec({ clOrdId: "hidden", execId: "qh", instrument: MSFT });
    expect(el.scrollTop).toBe(ROW_H * 17);
    expect(getByRole("button", { name: "5 new, resume" })).toBeDefined();

    // changing the filter is a new list: back to the top, nothing held, no resume button
    pick(container, "Instrument", "MSFT");
    expect(scroller(container).scrollTop).toBe(0);
    expect(container.querySelector(".bl-resume")).toBeNull();
    expect(ids(container)).toEqual(["hidden", "other"]);
  });
});

describe("N5: status cues and kind labels", () => {
  test("each status row shows its own glyph and its label; kinds read ORDER, ALLOC, CROSS", () => {
    const { container } = render(<Host />);
    up();
    const all = [
      S.ORD_STATUS_NEW,
      S.ORD_STATUS_PARTIALLY_FILLED,
      S.ORD_STATUS_FILLED,
      S.ORD_STATUS_CANCELED,
      S.ORD_STATUS_REJECTED,
      S.ORD_STATUS_PENDING_CANCEL,
      S.ORD_STATUS_PENDING_REPLACE,
    ];
    sendExec(
      ...all.map((status, i) => ({
        clOrdId: `s-${i}`,
        execId: `s${i}`,
        status,
      })),
    );
    sendExec({
      clOrdId: "s-0",
      execId: "sa",
      bookId: 3,
      allocation: true,
      lastQtyE2: 1n,
      bookCumQtyE2: 1n,
    });
    sendCross({ crossId: "x-k" });
    const cells = Array.from(
      container.querySelectorAll<HTMLElement>('[data-kind="order"] .c-status'),
    );
    expect(cells).toHaveLength(7);
    const glyphs = cells.map(
      (c) => c.querySelector("[aria-hidden]")?.textContent,
    );
    expect(glyphs.every((g) => g)).toBe(true);
    expect(new Set(glyphs).size).toBe(7);
    expect(new Set(cells.map((c) => c.textContent)).size).toBe(7);
    const kinds = Array.from(
      container.querySelectorAll<HTMLElement>(".bl-row[data-kind]"),
    ).map((r) => r.querySelectorAll('[role="cell"]')[2]?.textContent);
    expect(new Set(kinds)).toEqual(new Set(["ORDER", "ALLOC", "CROSS"]));
  });
});

describe("rows", () => {
  test("order, indented allocation children, and a cross linked to its residual", () => {
    const { container } = render(<Host />);
    up();
    sendExec(
      {
        clOrdId: "cl-412",
        execId: "E1",
        status: S.ORD_STATUS_PARTIALLY_FILLED,
        cumQtyE2: 310_000n,
        leavesQtyE2: 220_000n,
        lastPxE9: 241_200_000_000n,
        nettingCycleId: "run/1",
      },
      {
        clOrdId: "cl-412",
        execId: "E1",
        bookId: 3,
        allocation: true,
        status: S.ORD_STATUS_PARTIALLY_FILLED,
        cumQtyE2: 310_000n,
        leavesQtyE2: 220_000n,
        lastQtyE2: 110_000n,
        bookCumQtyE2: 110_000n,
        lastPxE9: 241_200_000_000n,
        nettingCycleId: "run/1",
      },
    );
    sendCross({ crossId: "x-1", nettingCycleId: "run/1" });
    sendCross({ crossId: "x-2", nettingCycleId: "DIRECT" });
    sendCross({ crossId: "x-3", nettingCycleId: "run/404" });
    sendCross({ crossId: "x-4", nettingCycleId: "" });
    const by = (kind: string) =>
      Array.from(
        container.querySelectorAll<HTMLElement>(`[data-kind="${kind}"]`),
      );
    const crossText = by("cross").map((r) => r.textContent ?? "");
    expect(crossText.find((t) => t.includes("x-1"))).toContain(
      "residual cl-412",
    );
    expect(crossText.find((t) => t.includes("x-2"))).toContain("transfer");
    expect(crossText.find((t) => t.includes("x-3"))).toContain("no residual");
    expect(crossText.find((t) => t.includes("x-4"))).toContain("no link");
    expect(crossText[0]).toContain("CROSS");
    const [order] = by("order");
    expect(order?.textContent).toContain("(net)");
    expect(order?.textContent).toContain("3,100.00"); // parent cum
    const [alloc] = by("alloc");
    expect(alloc?.textContent).toContain("1,100.00"); // the book's share and its own cum
    expect(alloc?.textContent).not.toContain("3,100.00");
    expect(alloc?.textContent).not.toContain("PARTIALLY_FILLED");
    // toggle hides the children
    const tog = container.querySelector<HTMLElement>(".bl-tog");
    expect(tog?.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(tog as HTMLElement);
    expect(by("alloc")).toHaveLength(0);
  });
});

describe("waiting and event age", () => {
  test("never delivered: waiting per subject, never stale; event subjects show last event age", () => {
    const { container } = render(<Host />);
    up();
    tick(300_000);
    // F3: event subjects never show a WAITING chip or a clock; they say "none since connect".
    expect(container.querySelector('.chip[data-state="waiting"]')).toBeNull();
    expect(container.querySelector('.chip[data-state="stale"]')).toBeNull();
    expect(container.querySelectorAll(".none-since").length).toBe(2); // exec in the head, crosses in the meta line
    expect(container.textContent).toContain("none since connect 12:04:09Z");
    expect(container.textContent).not.toMatch(/Waiting|WAITING/);
    expect(container.textContent).not.toContain("last event");

    sendExec({ clOrdId: "cl-1" });
    tick(600_000);
    expect(container.textContent).toContain("last event 10m00s ago");
    expect(container.querySelector('.chip[data-state="stale"]')).toBeNull();
    // crosses has still never delivered: "none since connect", not stale, not waiting
    expect(container.querySelector('.chip[data-state="waiting"]')).toBeNull();
    expect(container.querySelectorAll(".none-since")).toHaveLength(1);
  });

  test("the event subjects are never stale at any age", () => {
    const conn = {
      ...initialState().conn,
      status: "connected" as const,
      firstConnectedAtMs: 0,
      connectedAtMs: 0,
    };
    const entry = {
      value: ExecutionReport.fromPartial({}),
      receivedAtMs: 1,
    };
    for (const id of ["exec", "crosses"] as const) {
      expect(freshness(SUBJECTS[id], entry, conn, 86_400_000).state).toBe("ok");
      expect(freshness(SUBJECTS[id], null, conn, 86_400_000).state).toBe(
        "waiting",
      );
    }
  });
});

describe("wire text is text", () => {
  test("a cl_ord_id, reject reason, cross id and px policy render as literal text", () => {
    const evil = "<img src=x onerror=alert(1)>";
    const { container } = render(<Host />);
    up();
    sendExec({
      clOrdId: evil,
      status: S.ORD_STATUS_REJECTED,
      text: `<img onerror=alert(2)> ${evil}`,
    });
    sendCross({ crossId: evil, pxPolicyId: evil, nettingCycleId: evil });
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain(evil);
    expect(container.textContent).toContain("<img onerror=alert(2)>");
    expect(container.textContent).toContain("REJECTED");
  });
});

describe("instrument names, ClOrdID width, allocation headers", () => {
  const LONG = "18446744073709551615"; // a 20-digit ClOrdID
  const cellsOf = (row: Element) =>
    Array.from(row.querySelectorAll<HTMLElement>('[role="cell"]'));
  const send3 = () => {
    sendExec(
      {
        clOrdId: LONG,
        execId: "w1",
        nettingCycleId: "run/w",
        status: S.ORD_STATUS_PARTIALLY_FILLED,
      },
      {
        clOrdId: LONG,
        execId: "w1",
        bookId: 3,
        allocation: true,
        nettingCycleId: "run/w",
        lastQtyE2: 110_000n,
        bookCumQtyE2: 220_000n,
      },
    );
    sendCross({ crossId: "x-w", nettingCycleId: "run/w" });
  };

  test("orders, allocations and crosses label the instrument from reference data; the id stays in the tooltip", () => {
    const { container } = render(<Host />);
    up();
    sendExec({
      clOrdId: "unk",
      execId: "w2",
      instrument: { ...AAPL, instrumentId: 555 },
    });
    send3();
    for (const kind of ["order", "alloc", "cross"]) {
      const row = container.querySelector(`[data-kind="${kind}"]`);
      const instr = cellsOf(row as Element)[5];
      expect(instr?.textContent).toBe("AAPL");
      expect(
        instr?.querySelector('[title="instrument_id 1001"]'),
      ).not.toBeNull();
    }
    expect(container.textContent).toContain("id 555 (not in reference data)");
  });

  test("the ClOrdID column keeps the whole 20-digit id (and the tooltip carries it), on parents and allocations", () => {
    const { container } = render(<Host />);
    up();
    send3();
    for (const kind of ["order", "alloc"]) {
      const id = container
        .querySelector(`[data-kind="${kind}"] .c-id`)
        ?.querySelector(".wire-text");
      expect(id?.textContent).toBe(LONG);
      expect(id?.getAttribute("title")).toBe(LONG);
    }
    // jsdom has no layout: assert on the declared track. 20 digits + " (net)" must fit; the 4th track is ClOrdID.
    const cols = /grid-template-columns:([^;]+);/.exec(blotterCss)?.[1] ?? "";
    const tracks = cols
      .replace(/\/\*[^]*?\*\//g, "")
      .trim()
      .split(/\s+(?![^(]*\))/);
    expect(tracks).toHaveLength(13);
    expect(parseFloat(tracks[3] ?? "")).toBeGreaterThanOrEqual(
      (LONG + " (net)").length,
    );
    expect(tracks[3]).toMatch(/ch$/);
  });

  test("Detail takes the leftover width: it is the only flexible track", () => {
    const cols = /grid-template-columns:([^;]+);/.exec(blotterCss)?.[1] ?? "";
    expect(cols.match(/fr\b/g)).toHaveLength(1);
    expect(cols.trim()).toMatch(/minmax\(\d+ch, 1fr\)$/);
  });

  test("Qty and Filled headers name what an allocation row holds; allocation cells match", () => {
    const { container } = render(<Host />);
    up();
    send3();
    const heads = Array.from(
      container.querySelectorAll<HTMLElement>('[role="columnheader"]'),
    );
    const qty = heads.find((h) => h.textContent?.includes("Fill qty"));
    const filled = heads.find((h) => h.textContent?.includes("Book filled"));
    expect(qty?.textContent).toBe("Qty / Fill qty");
    expect(filled?.textContent).toBe("Filled / Book filled");
    // the parent's meaning is still on the header
    expect(qty?.getAttribute("title")).toContain("ORDER");
    expect(filled?.getAttribute("title")).toContain("cumulative filled");
    const a = cellsOf(
      container.querySelector('[data-kind="alloc"]') as Element,
    );
    expect(a[7]?.textContent).toBe("1,100.00"); // Fill qty = last_qty_e2
    expect(a[7]?.getAttribute("title")).toContain("last_qty_e2");
    expect(a[10]?.textContent).toBe("2,200.00"); // Book filled = book_cum_qty_e2
    expect(a[10]?.getAttribute("title")).toContain("book_cum_qty_e2");
  });
});

describe("cross values in a scrolling region", () => {
  const UUID = "0198f5c2-7a3e-7c41-9b0d-4e6f1a2b3c4d"; // a 36-character cross id
  const LONG = "18446744073709551615"; // a 20-digit ClOrdID
  const css = blotterCss.replace(/\/\*[^]*?\*\//g, "");
  const tracks = (/grid-template-columns:([^;]+);/.exec(css)?.[1] ?? "")
    .trim()
    .split(/\s+(?![^(]*\))/);
  const sendLinked = () => {
    sendExec({
      clOrdId: LONG,
      execId: "v1",
      nettingCycleId: "run/v",
      status: S.ORD_STATUS_NEW,
    });
    sendCross({ crossId: UUID, nettingCycleId: "run/v" });
  };

  test("a cross's id and residual link are not ellipsized and carry the full value in a title", () => {
    const { container } = render(<Host />);
    up();
    sendLinked();
    const row = container.querySelector('[data-kind="cross"]');
    const id = row?.querySelector(".c-id");
    const status = row?.querySelector(".c-status");
    for (const cell of [id, status])
      expect(cell?.classList.contains("c-full")).toBe(true);
    expect(id?.querySelector(".wire-text")?.getAttribute("title")).toBe(UUID);
    expect(status?.textContent).toBe(`residual ${LONG}`);
    expect(status?.querySelector(".wire-text")?.getAttribute("title")).toBe(
      LONG,
    );
    // jsdom has no layout: assert on the declared tracks and the one ellipsis rule.
    expect(parseFloat(tracks[3] ?? "")).toBeGreaterThanOrEqual(UUID.length);
    expect(parseFloat(tracks[9] ?? "")).toBeGreaterThanOrEqual(
      `residual ${LONG}`.length,
    );
    const withEllipsis = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].filter(
      (m) => /text-overflow/.test(m[2] ?? ""),
    );
    expect(withEllipsis.map((m) => m[1]?.trim())).toEqual([
      ".bl-cols > :not(.c-full)",
    ]);
  });

  test("the table is an XScroll region named for it, focusable only while it overflows", () => {
    const sw = Object.getOwnPropertyDescriptor(
      HTMLElement.prototype,
      "scrollWidth",
    );
    const cw = Object.getOwnPropertyDescriptor(
      HTMLElement.prototype,
      "clientWidth",
    );
    const widths = { scroll: 2000, client: 1200 };
    Object.defineProperty(HTMLElement.prototype, "scrollWidth", {
      configurable: true,
      get: () => widths.scroll,
    });
    Object.defineProperty(HTMLElement.prototype, "clientWidth", {
      configurable: true,
      get: () => widths.client,
    });
    try {
      const { container, getByRole } = render(<Host />);
      up();
      sendLinked();
      const region = getByRole("region", { name: "Blotter table" });
      expect(region.classList.contains("xscroll")).toBe(true);
      expect(region.getAttribute("tabindex")).toBe("0");
      // the header scrolls with the body: both sit inside the one region
      expect(region.querySelector('[role="columnheader"]')).not.toBeNull();
      expect(region.querySelector(".bl-scroll")).toBe(
        container.querySelector(".bl-scroll"),
      );
    } finally {
      if (sw) Object.defineProperty(HTMLElement.prototype, "scrollWidth", sw);
      if (cw) Object.defineProperty(HTMLElement.prototype, "clientWidth", cw);
    }
  });

  test("the header grid and the body grid use the same track list", () => {
    const { container } = render(<Host />);
    up();
    sendLinked();
    const head = container.querySelector('.bl-head [role="row"]');
    const body = container.querySelector('.bl-body [role="row"]');
    expect(head?.classList.contains("bl-cols")).toBe(true);
    expect(body?.classList.contains("bl-cols")).toBe(true);
    // one declaration, on .bl-cols, with no override for the header or the body
    expect(css.match(/grid-template-columns/g)).toHaveLength(1);
    expect(tracks).toHaveLength(13);
    expect(head?.querySelectorAll('[role="columnheader"]').length).toBe(
      tracks.length,
    );
    expect(body?.querySelectorAll('[role="cell"]').length).toBe(tracks.length);
  });
});
