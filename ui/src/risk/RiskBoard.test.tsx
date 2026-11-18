import { act, cleanup, render, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import {
  SUBJECTS,
  keyOf,
  type SubjectId,
  type WithMeta,
} from "../bus/subjects";
import {
  HealthStatus,
  PositionSnapshot,
  ReconAlert,
  RiskLimitAlert,
  RiskSnapshot,
  RiskSnapshot_LimitLevel as L,
  TargetPosition,
  ValuationSnapshot,
} from "../gen/live";
import { busSink, flushBus, resetBus } from "../store";
import { ink, loadCss, token } from "../testCss";
import { RiskBoard } from "./RiskBoard";

const T0 = Date.UTC(2026, 10, 8, 12, 4, 9);
beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(T0);
  resetBus();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const up = () =>
  act(() =>
    busSink.onStatus(
      "connected",
      { attempt: 0, nextRetryAtMs: null },
      Date.now(),
    ),
  );
const tick = (ms: number) => act(() => void vi.advanceTimersByTime(ms));
function send(id: SubjectId, subject: string, msg: WithMeta) {
  act(() => {
    busSink.onMessage(
      id,
      subject,
      keyOf(SUBJECTS[id].pattern, subject),
      msg,
      Date.now(),
    );
    flushBus();
  });
}
const ins = (id: number, symbol: string) => ({
  instrumentId: id,
  symbol,
  instrumentClass: 1,
  currency: "USD",
});
const panel = (c: HTMLElement, id: string) => {
  const el = c.querySelector(`section[aria-labelledby="${id}"]`);
  if (!el) throw new Error(`no panel ${id}`);
  return el as HTMLElement;
};
const chips = (el: Element, state: string) =>
  el.querySelectorAll(`.chip[data-state="${state}"]`).length;

const risk = (id: string) =>
  RiskSnapshot.fromPartial({
    meta: { msgId: id },
    greeks: [
      {
        underlying: ins(1001, "AAPL"),
        firmDeltaQtyE2: 1_230_000n,
        notionalE9: 2_411_820_000_000_000n,
        vegaE9: 62_000_000_000_000n,
        vegaLevel: L.LIMIT_LEVEL_WARN,
        vegaWarnE9: 50_000_000_000_000n,
        vegaHardE9: 100_000_000_000_000n,
        gammaQtyE2: 1_240_000n,
        gammaLevel: L.LIMIT_LEVEL_OK,
        gammaWarnQtyE2: 2_000_000n,
        gammaHardQtyE2: 5_000_000n,
        rhoE9: -3_100_000_000_000n,
        rhoLevel: L.LIMIT_LEVEL_UNSPECIFIED,
        greeksFlagged: true,
        limitDataSuspect: true,
      },
      {
        underlying: ins(1002, "MSFT"),
        vegaE9: 104_500_000_000_000n,
        vegaLevel: L.LIMIT_LEVEL_HARD,
        vegaWarnE9: 50_000_000_000_000n,
        vegaHardE9: 100_000_000_000_000n,
      },
    ],
  });

describe("risk board states", () => {
  test("connected, nothing received: cadence panels WAITING, no number, no OK", () => {
    const { container } = render(<RiskBoard />);
    up();
    for (const id of ["p-firm", "p-books", "p-band"]) {
      const p = panel(container, id);
      expect(chips(p, "waiting")).toBe(1);
      expect(p.textContent).toContain("Waiting for the first");
      expect(p.querySelector("table")).toBeNull();
    }
    expect(container.querySelector('.chip[data-state="ok"]')).toBeNull();
  });

  test("F3: an empty event panel reads 'none since connect', never a WAITING chip, and its clock never ticks", () => {
    const { container } = render(<RiskBoard />);
    up();
    const alerts = panel(container, "p-alerts");
    expect(chips(alerts, "waiting")).toBe(0);
    expect(alerts.querySelector(".chip")).toBeNull();
    expect(alerts.textContent).toContain("none since connect 12:04:09Z");
    expect(alerts.textContent).not.toMatch(/waiting/i);
    // exo.alerts.recon is an event subject too: its line sits in the Books panel.
    expect(panel(container, "p-books").textContent).toContain(
      "exo.alerts.recon: none since connect 12:04:09Z",
    );
    const before = container.textContent;
    tick(65_000);
    expect(alerts.textContent).toContain("none since connect 12:04:09Z");
    expect(chips(alerts, "waiting")).toBe(0);
    expect(alerts.textContent).not.toMatch(/\d+m\d\ds|WAITING/);
    // the cadence panels did tick: only the event panels are frozen
    expect(chips(panel(container, "p-firm"), "waiting")).toBe(1);
    expect(container.textContent).not.toBe(before);
  });

  test("risk subject: waiting is not stale, and stale keeps the last value", () => {
    const { container } = render(<RiskBoard />);
    up();
    tick(10_000); // a long wait never becomes stale
    const firm = () => panel(container, "p-firm");
    expect(chips(firm(), "waiting")).toBe(1);
    expect(chips(firm(), "stale")).toBe(0);
    expect(firm().textContent).toContain("WAITING 10s");

    send("riskFirm", "d1.risk.firm", risk("r1"));
    expect(chips(firm(), "stale")).toBe(0);
    expect(firm().textContent).toContain("last msg 0s ago");
    tick(4_000);
    expect(firm().getAttribute("data-fresh")).toBe("stale");
    expect(firm().textContent).toContain("STALE 4s");
    expect(firm().textContent).toContain("AAPL"); // last value kept
    expect(chips(firm(), "waiting")).toBe(3); // cells whose level was never populated (AAPL rho, MSFT gamma + rho)
  });

  test("fixture snapshot renders chips, bars, flags and limit n/a", () => {
    const { container } = render(<RiskBoard />);
    up();
    send("riskFirm", "d1.risk.firm", risk("r2"));
    const firm = panel(container, "p-firm");
    const [aapl, msft] = Array.from(firm.querySelectorAll("tbody tr"));
    if (!aapl || !msft) throw new Error("rows missing");
    const a = within(aapl as HTMLElement);
    expect(a.getByText("WARN")).toBeDefined();
    expect(a.getByText("OK")).toBeDefined();
    expect(a.getByText("WAITING")).toBeDefined(); // rho level UNSPECIFIED: not OK
    expect(a.getByText("DATA SUSPECT")).toBeDefined();
    expect(a.getByText("62,000")).toBeDefined();
    expect(a.getByText("−3,100")).toBeDefined();
    expect(aapl.querySelector(".flag")).not.toBeNull();
    // rho has no cap on this fixture: bar replaced by text; vega bar says 62%
    expect(a.getByText("limit n/a")).toBeDefined();
    expect(
      aapl.querySelector('[role="img"][aria-label="62% of hard limit"]'),
    ).not.toBeNull();
    // MSFT: HARD chip, bar beyond hard with the mark; gamma/rho have no caps
    const m = within(msft as HTMLElement);
    expect(m.getByText("HARD")).toBeDefined();
    expect(
      msft.querySelector('[aria-label="100% of hard limit, beyond hard"]'),
    ).not.toBeNull();
    expect(m.getAllByText("limit n/a")).toHaveLength(2);
    // Delta carries no limit chip: 3 limit chips per row at most
    expect(aapl.querySelectorAll(".chip")).toHaveLength(3);
  });

  test("stale cell keeps its last WARN as a trailing glyph", () => {
    const { container } = render(<RiskBoard />);
    up();
    send("riskFirm", "d1.risk.firm", risk("r3"));
    tick(4_000);
    const msft = panel(container, "p-firm").querySelectorAll("tbody tr")[1];
    expect(msft?.querySelector(".trail")?.textContent).toContain(
      "last level HARD",
    );
  });
});

describe("books, targets, alerts", () => {
  test("valuation row shows model, paths, seed and a high-SE flag with text", () => {
    const { container } = render(<RiskBoard />);
    up();
    send(
      "valuations",
      "exo.valuations.3",
      ValuationSnapshot.fromPartial({
        meta: { msgId: "v1" },
        bookId: 3,
        valuation: { modelId: "heston-qe-v1", seed: 20260n, nPaths: 200000 },
        lines: [
          {
            underlying: ins(1001, "AAPL"),
            deltaQtyE2: 100n,
            vegaE9: 5_000_000_000n,
            flaggedGreeks: ["vega", "gamma"],
          },
          {
            underlying: ins(1001, "AAPL"),
            deltaQtyE2: 250n,
            vegaE9: 1_000_000_000n,
          },
        ],
      }),
    );
    const row = panel(container, "p-books").querySelector("tbody tr");
    const t = row?.textContent ?? "";
    expect(t).toContain("heston-qe-v1");
    expect(t).toContain("200,000");
    expect(t).toContain("20260");
    expect(t).toContain("3.50"); // 100n + 250n qty_e2 summed per underlying
    expect(t).toContain("high SE: gamma, vega");
  });

  test("target absent reads 'no target yet'; position and gap are dashes when unreported", () => {
    const { container } = render(<RiskBoard />);
    up();
    send(
      "positions",
      "d1.positions.1",
      PositionSnapshot.fromPartial({
        meta: { msgId: "p1" },
        bookId: 1,
        lines: [
          {
            instrument: ins(1001, "AAPL"),
            qtyE2: 430_000n,
            inflightQtyE2: 50_000n,
          },
        ],
      }),
    );
    send(
      "targets",
      "exo.targets.3.1001",
      TargetPosition.fromPartial({
        meta: { msgId: "t1" },
        bookId: 3,
        instrument: ins(1001, "AAPL"),
        targetQtyE2: 800_000n,
        bandQtyE2: 50_000n,
      }),
    );
    const [b1, b3] = Array.from(
      panel(container, "p-band").querySelectorAll("tbody tr"),
    );
    expect(b1?.textContent).toContain("no target yet");
    expect(b1?.textContent).toContain("4,300.00");
    expect(b3?.textContent).not.toContain("no target yet");
    expect(b3?.textContent).toContain("8,000.00");
    expect(b3?.textContent).toContain("–"); // position unreported, gap unknown
  });

  test("alerts: newest first, capped at the ring size, event-driven (never stale)", () => {
    const { container } = render(<RiskBoard />);
    up();
    const n = SUBJECTS.limitAlerts.ringSize + 5;
    for (let i = 0; i < n; i++)
      send(
        "limitAlerts",
        "d1.alerts.limits",
        RiskLimitAlert.fromPartial({
          meta: { msgId: `a${i}` },
          breached: true,
          text: `alert ${i}`,
        }),
      );
    tick(600_000);
    const p = panel(container, "p-alerts");
    const items = p.querySelectorAll("li");
    expect(items).toHaveLength(SUBJECTS.limitAlerts.ringSize);
    expect(items[0]?.textContent).toContain(`alert ${n - 1}`);
    expect(chips(p, "stale")).toBe(0);
    expect(p.textContent).toContain("last event 10m00s ago");
  });

  test("alert chips: breached HARD, warning WARN, cleared OK", () => {
    const { container } = render(<RiskBoard />);
    up();
    send(
      "limitAlerts",
      "d1.alerts.limits",
      RiskLimitAlert.fromPartial({
        meta: { msgId: "c1" },
        cleared: true,
        text: "back",
      }),
    );
    send(
      "limitAlerts",
      "d1.alerts.limits",
      RiskLimitAlert.fromPartial({ meta: { msgId: "c2" }, text: "warn" }),
    );
    send(
      "limitAlerts",
      "d1.alerts.limits",
      RiskLimitAlert.fromPartial({
        meta: { msgId: "c3" },
        breached: true,
        text: "hard",
      }),
    );
    const states = Array.from(
      panel(container, "p-alerts").querySelectorAll("li .chip"),
    ).map((c) => c.getAttribute("data-state"));
    expect(states).toEqual(["hard", "warn", "ok"]);
  });
});

describe("wire text is text", () => {
  const payload = "<img src=x onerror=\"document.title='pwned'\">";
  test("an alert reason renders as literal text, no element is created", () => {
    const { container } = render(<RiskBoard />);
    up();
    send(
      "limitAlerts",
      "d1.alerts.limits",
      RiskLimitAlert.fromPartial({ meta: { msgId: "x1" }, text: payload }),
    );
    expect(container.querySelector("img")).toBeNull();
    const w = panel(container, "p-alerts").querySelector(".wire-text");
    expect(w?.textContent).toBe(payload);
    expect(w?.getAttribute("title")).toBe(payload);
    expect(w?.children).toHaveLength(0);
    expect(document.title).not.toBe("pwned");
  });
});

// ---- Books: HALTED from the wire, WAITING for books that never published ----------------------------

const sendHealth = (
  id: "d1Health" | "exoHealth",
  detail: Record<string, string>,
) =>
  send(
    id,
    id === "d1Health" ? "d1.health" : "exo.health",
    HealthStatus.fromPartial({
      meta: { msgId: `h-${Math.random()}` },
      component: id === "d1Health" ? "delta-one" : "exo",
      ok: true,
      detail,
    }),
  );
const sendRecon = (halted: boolean, bookId = 41) =>
  send(
    "reconAlerts",
    "exo.alerts.recon",
    ReconAlert.fromPartial({
      meta: { msgId: `ra-${Math.random()}` },
      bookId,
      instrument: ins(1001, "AAPL"),
      exoViewQtyE2: 500n,
      d1ViewQtyE2: 300n,
      publishingHalted: halted,
    }),
  );
const sendVal = (bookId: number) =>
  send(
    "valuations",
    `exo.valuations.${bookId}`,
    ValuationSnapshot.fromPartial({
      meta: { msgId: `v-${Math.random()}` },
      bookId,
      valuation: { modelId: "heston-qe-v1", seed: 1n, nPaths: 1000 },
      lines: [{ underlying: ins(1001, "AAPL"), deltaQtyE2: 100n }],
    }),
  );
const bookRows = (c: HTMLElement) =>
  Array.from(panel(c, "p-books").querySelectorAll("tbody tr"));
const rowOf = (c: HTMLElement, book: number) => {
  const r = bookRows(c).find(
    (x) => x.querySelector("th")?.textContent === `book ${book}`,
  );
  if (!r) throw new Error(`no row for book ${book}`);
  return r;
};
const chipOf = (r: Element) => r.querySelector(".chip");

describe("F1: HALTED reaches the books table", () => {
  test("exo.health halted:recon and halted:no-snapshot show HALTED with the reason after the label", () => {
    const { container } = render(<RiskBoard />);
    up();
    sendVal(41);
    sendVal(42);
    sendHealth("exoHealth", {
      "book.41.state": "halted:recon",
      "book.42.state": "halted:no-snapshot",
    });
    for (const [book, reason] of [
      [41, "recon"],
      [42, "no-snapshot"],
    ] as const) {
      const r = rowOf(container, book);
      expect(chipOf(r)?.getAttribute("data-state")).toBe("halted");
      expect(chipOf(r)?.textContent).toContain(`HALTED ${reason}`);
      expect(r.getAttribute("data-fresh")).toBe("halted");
      expect(r.textContent).toContain("1.00"); // the last value is kept
    }
  });

  test("a recon alert alone halts the book; the cleared alert releases it", () => {
    const { container } = render(<RiskBoard />);
    up();
    sendVal(41);
    sendRecon(true);
    const r = () => rowOf(container, 41);
    expect(chipOf(r())?.textContent).toContain("HALTED recon");
    expect(r().querySelector("[title]")?.getAttribute("title")).toContain(
      "EXO view 5.00, D1 view 3.00",
    );
    expect(panel(container, "p-books").textContent).toContain(
      "exo.alerts.recon: 1 since connect, last 12:04:09Z",
    );
    sendRecon(false);
    expect(chipOf(r())?.getAttribute("data-state")).toBe("ok");
  });

  test("precedence: HALTED outranks STALE, the stale age moves to the tooltip", () => {
    const { container } = render(<RiskBoard />);
    up();
    sendVal(41);
    sendVal(42);
    sendHealth("exoHealth", { "book.41.state": "halted:recon" });
    tick(13_000); // > the 12 s valuation threshold
    const halted = rowOf(container, 41);
    const stale = rowOf(container, 42);
    expect(chipOf(halted)?.getAttribute("data-state")).toBe("halted");
    expect(chipOf(stale)?.getAttribute("data-state")).toBe("stale");
    expect(halted.querySelector(".chip[data-state='stale']")).toBeNull();
    expect(halted.querySelector("[title]")?.getAttribute("title")).toContain(
      "also STALE 13s",
    );
  });

  test("a live exo.health state outvotes an old halted alert (an EXO restart clears a halt silently)", () => {
    const { container } = render(<RiskBoard />);
    up();
    sendVal(41);
    sendRecon(true);
    sendHealth("exoHealth", { "book.41.state": "publishing" });
    expect(chipOf(rowOf(container, 41))?.getAttribute("data-state")).toBe("ok");
  });

  test("a halted book that has not published a valuation shows HALTED, not WAITING", () => {
    const { container } = render(<RiskBoard />);
    up();
    sendHealth("exoHealth", { "book.41.state": "halted:no-snapshot" });
    const r = rowOf(container, 41);
    expect(chipOf(r)?.textContent).toContain("HALTED no-snapshot");
    expect(r.textContent).toContain("no valuation yet");
  });
});

describe("F2: books on the wire with no valuation are WAITING", () => {
  test("exo.health and d1.health book keys make rows; WAITING, dashes, a clock, never a zero", () => {
    const { container } = render(<RiskBoard />);
    up();
    sendVal(41);
    sendHealth("exoHealth", {
      "book.41.state": "publishing",
      "book.57.state": "awaiting_marks",
    });
    sendHealth("d1Health", {
      "limits.book.41.rejects": "0",
      "limits.book.63.rejects": "0",
      "limits.book.63.valuation": "none",
    });
    tick(10_000);
    expect(
      bookRows(container).map((r) => r.querySelector("th")?.textContent),
    ).toEqual(["book 41", "book 57", "book 63"]);
    for (const book of [57, 63]) {
      const r = rowOf(container, book);
      expect(chipOf(r)?.getAttribute("data-state")).toBe("waiting");
      expect(chipOf(r)?.textContent).toBe("WAITING 10s");
      expect(r.textContent).toContain("no valuation yet");
      const cells = Array.from(r.querySelectorAll("td.num"));
      expect(cells.length).toBeGreaterThan(0);
      for (const c of cells) expect(c.textContent).toBe("–");
    }
    expect(rowOf(container, 57).textContent).toContain("awaiting_marks");
    expect(chipOf(rowOf(container, 41))?.getAttribute("data-state")).toBe("ok");
  });
});

describe("F6: limit values are tinted by level, live values only", () => {
  test("WARN value in warn ink, HARD value in hard ink at weight 600, OK and waiting untinted", () => {
    const off = loadCss("risk", "shell");
    try {
      const { container } = render(<RiskBoard />);
      up();
      send("riskFirm", "d1.risk.firm", risk("f6-risk"));
      const [aapl, msft] = Array.from(
        panel(container, "p-firm").querySelectorAll("tbody tr"),
      );
      const vals = (r: Element | undefined) =>
        Array.from(r?.querySelectorAll(".limit-cell > .num") ?? []);
      const [aVega, aGamma, aRho] = vals(aapl);
      const [mVega] = vals(msft);
      expect(aVega?.getAttribute("data-level")).toBe("warn");
      expect(aGamma?.getAttribute("data-level")).toBeNull(); // OK
      expect(aRho?.getAttribute("data-level")).toBeNull(); // level not populated
      expect(mVega?.getAttribute("data-level")).toBe("hard");
      expect(ink(aVega ?? null)).toBe("var(--state-warn-fg)");
      expect(ink(mVega ?? null)).toBe("var(--state-hard-fg)");
      expect(getComputedStyle(mVega as Element).fontWeight).toBe(
        "var(--fw-bold)",
      );
      expect(token("--fw-bold")).toBe("600");
      expect(getComputedStyle(aVega as Element).fontWeight).not.toBe(
        "var(--fw-bold)",
      );
      // stale: the value keeps its last number but drops the hue
      tick(4_000);
      for (const v of vals(msft))
        expect(v.getAttribute("data-level")).toBeNull();
    } finally {
      off();
    }
  });
});

describe("F7: no firm-level rehedge band column", () => {
  test("the firm table has no band column and every row matches the header", () => {
    const { container } = render(<RiskBoard />);
    up();
    send("riskFirm", "d1.risk.firm", risk("f7-risk"));
    const firm = panel(container, "p-firm");
    const heads = Array.from(firm.querySelectorAll("thead th")).map(
      (h) => h.textContent,
    );
    expect(heads.join("|")).not.toMatch(/band/i);
    expect(heads).toHaveLength(8);
    for (const r of Array.from(firm.querySelectorAll("tbody tr")))
      expect(r.children).toHaveLength(heads.length);
    expect(firm.textContent).not.toContain("No firm-level rehedge band");
  });
});
