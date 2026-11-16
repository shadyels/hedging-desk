import { act, cleanup, render, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import {
  SUBJECTS,
  keyOf,
  type SubjectId,
  type WithMeta,
} from "../bus/subjects";
import {
  PositionSnapshot,
  RiskLimitAlert,
  RiskSnapshot,
  RiskSnapshot_LimitLevel as L,
  TargetPosition,
  ValuationSnapshot,
} from "../gen/live";
import { busSink, flushBus, resetBus } from "../store";
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
  test("connected, nothing received: every panel WAITING, no number, no OK", () => {
    const { container } = render(<RiskBoard />);
    up();
    for (const id of ["p-firm", "p-books", "p-band", "p-alerts"]) {
      const p = panel(container, id);
      expect(chips(p, "waiting")).toBe(1);
      expect(p.textContent).toContain("Waiting for the first");
      expect(p.querySelector("table")).toBeNull();
    }
    expect(container.querySelector('.chip[data-state="ok"]')).toBeNull();
    expect(panel(container, "p-alerts").textContent).toContain(
      "No limit alerts since connect 12:04:09Z",
    );
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
