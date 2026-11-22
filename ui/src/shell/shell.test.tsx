import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { App } from "../App";
import {
  SUBJECTS,
  keyOf,
  type SubjectId,
  type WithMeta,
} from "../bus/subjects";
import {
  HealthStatus,
  PositionSnapshot,
  RiskSnapshot,
  RiskSnapshot_LimitLevel as L,
  ValuationSnapshot,
} from "../gen/live";
import { busSink, flushBus, resetBus } from "../store";
import { PRECEDENCE, rollup, type Finding } from "./rollup";
import { SCREENS, screenFromHash } from "./nav";
import type { ChipState } from "../components/StateChip";

const T0 = Date.UTC(2026, 10, 8, 12, 4, 9);
beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(T0);
  resetBus();
  window.location.hash = "";
  // This Node's own experimental localStorage shadows jsdom's and is absent: install a real in-memory one.
  const store = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
    },
  });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  window.location.hash = "";
  document.documentElement.removeAttribute("data-theme");
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
const AAPL = {
  instrumentId: 1001,
  symbol: "AAPL",
  instrumentClass: 1,
  currency: "USD",
};
let n = 0;
const meta = () => ({ msgId: `m${n++}` });
const health = (id: "d1Health" | "exoHealth", ok = true, detail = {}) =>
  send(
    id,
    id === "d1Health" ? "d1.health" : "exo.health",
    HealthStatus.fromPartial({ meta: meta(), component: id, ok, detail }),
  );
const riskSnap = (vega = L.LIMIT_LEVEL_OK) =>
  send(
    "riskFirm",
    "d1.risk.firm",
    RiskSnapshot.fromPartial({
      meta: meta(),
      greeks: [
        {
          underlying: AAPL,
          vegaLevel: vega,
          gammaLevel: L.LIMIT_LEVEL_OK,
          rhoLevel: L.LIMIT_LEVEL_OK,
        },
      ],
    }),
  );
/** Every cadence subject the roll-up reads, fresh. */
function allFresh(vega = L.LIMIT_LEVEL_OK) {
  health("d1Health");
  health("exoHealth");
  riskSnap(vega);
  send(
    "valuations",
    "exo.valuations.1",
    ValuationSnapshot.fromPartial({ meta: meta(), bookId: 1 }),
  );
  send(
    "positions",
    "d1.positions.1",
    PositionSnapshot.fromPartial({ meta: meta(), bookId: 1 }),
  );
}
const trustChip = (c: HTMLElement) => {
  const chip = c.querySelector(".bar-trust .chip");
  if (!chip) throw new Error("no roll-up chip");
  return chip;
};
const tabs = () => within(screen.getByRole("tablist")).getAllByRole("tab");
const selected = () =>
  tabs()
    .filter((t) => t.getAttribute("aria-selected") === "true")
    .map((t) => t.textContent);

describe("N1: tabs and the URL hash", () => {
  test("four tabs, one screen at a time, Risk by default", () => {
    render(<App />);
    expect(tabs().map((t) => t.textContent)).toEqual([
      "Risk",
      "Tracker",
      "Blotter",
      "Health",
    ]);
    expect(selected()).toEqual(["Risk"]);
    expect(screen.getAllByRole("tabpanel")).toHaveLength(1);
    expect(screen.getByRole("tabpanel").id).toBe("panel-risk");
    expect(document.querySelector("#p-firm")).not.toBeNull();
    expect(document.querySelector(".tgrid, .tracker")).toBeNull();
    expect(screen.queryByRole("table", { name: "Bus subjects" })).toBeNull();
  });

  test("clicking a tab switches the screen and writes the hash", () => {
    render(<App />);
    fireEvent.click(screen.getByRole("tab", { name: "Blotter" }));
    expect(window.location.hash).toBe("#blotter");
    expect(selected()).toEqual(["Blotter"]);
    expect(document.querySelector("#p-blotter")).not.toBeNull();
    expect(document.querySelector("#p-firm")).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: "Health" }));
    expect(window.location.hash).toBe("#health");
    expect(screen.getByRole("table", { name: "Bus subjects" })).toBeDefined();
  });

  test("the hash survives a refresh: a new mount opens the hashed screen", () => {
    window.location.hash = "#tracker";
    render(<App />);
    expect(selected()).toEqual(["Tracker"]);
    expect(screen.getByRole("tabpanel").id).toBe("panel-tracker");
    cleanup();
    window.location.hash = "#nonsense";
    render(<App />);
    expect(selected()).toEqual(["Risk"]);
  });

  test("an external hash change (back button, pasted link) follows", () => {
    render(<App />);
    act(() => {
      window.location.hash = "#health";
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    expect(selected()).toEqual(["Health"]);
  });

  test("screenFromHash maps exactly the four ids and defaults to risk", () => {
    for (const s of SCREENS) expect(screenFromHash(`#${s.id}`)).toBe(s.id);
    for (const bad of ["", "#", "#Risk", "risk", "#risk/x", "#__proto__"])
      expect(screenFromHash(bad)).toBe("risk");
  });

  test("tablist semantics: aria-selected, aria-controls, one tab stop (roving tabindex)", () => {
    render(<App />);
    expect(screen.getByRole("tablist").getAttribute("aria-label")).toBe(
      "Screens",
    );
    for (const t of tabs()) {
      const sel = t.getAttribute("aria-selected") === "true";
      expect(t.getAttribute("tabindex")).toBe(sel ? "0" : "-1");
      expect(t.getAttribute("aria-controls")).toBe(
        `panel-${t.id.replace("tab-", "")}`,
      );
    }
  });

  test("arrow keys move between tabs (wrapping) and Home/End jump; focus follows", () => {
    render(<App />);
    const risk = screen.getByRole("tab", { name: "Risk" });
    risk.focus();
    fireEvent.keyDown(risk, { key: "ArrowRight" });
    expect(selected()).toEqual(["Tracker"]);
    expect(document.activeElement?.id).toBe("tab-tracker");
    fireEvent.keyDown(document.activeElement as Element, { key: "ArrowLeft" });
    fireEvent.keyDown(document.activeElement as Element, { key: "ArrowLeft" });
    expect(selected()).toEqual(["Health"]); // wrapped from the first tab to the last
    expect(window.location.hash).toBe("#health");
    fireEvent.keyDown(document.activeElement as Element, { key: "Home" });
    expect(selected()).toEqual(["Risk"]);
    fireEvent.keyDown(document.activeElement as Element, { key: "End" });
    expect(selected()).toEqual(["Health"]);
    fireEvent.keyDown(document.activeElement as Element, { key: "a" });
    expect(selected()).toEqual(["Health"]);
  });

  test("the bus subjects table is on the Health screen, not under the other three", () => {
    for (const s of ["risk", "tracker", "blotter"]) {
      window.location.hash = `#${s}`;
      render(<App />);
      expect(screen.queryByRole("table", { name: "Bus subjects" })).toBeNull();
      cleanup();
    }
    window.location.hash = "#health";
    render(<App />);
    expect(screen.getByRole("table", { name: "Bus subjects" })).toBeDefined();
  });
});

describe("shell bar", () => {
  test("N8: the product name is the one h1, and every heading nests under it on every screen", () => {
    for (const s of SCREENS) {
      window.location.hash = `#${s.id}`;
      const { container } = render(<App />);
      up();
      const hs = Array.from(
        container.querySelectorAll<HTMLElement>("h1,h2,h3,h4,h5,h6"),
      );
      expect(
        hs.filter((h) => h.tagName === "H1").map((h) => h.textContent),
      ).toEqual(["Desk Hedging"]);
      expect(hs[0]?.tagName).toBe("H1");
      let prev = 0;
      for (const h of hs) {
        const level = Number(h.tagName.slice(1));
        expect(level, `${s.id}: ${h.textContent}`).toBeLessThanOrEqual(
          prev + 1,
        );
        prev = level;
      }
      expect(hs.some((h) => h.tagName === "H2")).toBe(true);
      cleanup();
    }
  });

  test("UTC clock ticks locally", () => {
    const { container } = render(<App />);
    expect(container.querySelector(".bar-clock")?.textContent).toBe(
      "12:04:09Z",
    );
    tick(3_000);
    expect(container.querySelector(".bar-clock")?.textContent).toBe(
      "12:04:12Z",
    );
  });

  test("theme toggle: dark by default, light sets data-theme on the root, and it is remembered", () => {
    render(<App />);
    const root = document.documentElement;
    expect(root.dataset["theme"]).toBe("dark");
    fireEvent.click(screen.getByRole("button", { name: "dark" }));
    expect(root.dataset["theme"]).toBe("light");
    expect(window.localStorage.getItem("hedging-ui.theme")).toBe("light");
    cleanup();
    render(<App />); // a refresh
    expect(root.dataset["theme"]).toBe("light");
    fireEvent.click(screen.getByRole("button", { name: "light" }));
    expect(root.dataset["theme"]).toBe("dark");
  });
});

describe("roll-up: the worst state across the screens", () => {
  const f = (state: ChipState, label?: string): Finding => ({
    state,
    why: `${state} finding`,
    ...(label ? { label } : {}),
  });

  test("pure: the worst of any mix wins, in the DESIGN.md order", () => {
    expect(PRECEDENCE).toEqual([
      "disconnected",
      "waiting",
      "halted",
      "stale",
      "hard",
      "warn",
      "ok",
    ]);
    for (let i = 0; i < PRECEDENCE.length; i++) {
      for (let j = i; j < PRECEDENCE.length; j++) {
        const a = PRECEDENCE[i] as ChipState;
        const b = PRECEDENCE[j] as ChipState;
        expect(rollup([f(b), f(a)]).state).toBe(a); // order of arrival does not matter
        expect(rollup([f(a), f(b)]).state).toBe(a);
      }
    }
  });

  test("pure: nothing, or only OK, is OK with no reasons; otherwise the reasons list every non-OK finding", () => {
    expect(rollup([])).toEqual({ state: "ok", label: "OK", reasons: [] });
    expect(rollup([f("ok")]).reasons).toEqual([]);
    const r = rollup([f("warn"), f("ok"), f("stale"), f("warn")]);
    expect(r.state).toBe("stale");
    expect(r.reasons).toEqual([
      "stale finding",
      "warn finding",
      "warn finding",
    ]);
  });

  test("pure: a label override (FAULT) applies to the winning finding only", () => {
    expect(rollup([f("hard", "FAULT"), f("warn")]).label).toBe("FAULT");
    expect(rollup([f("hard", "FAULT"), f("stale")]).label).toBe("STALE");
    expect(rollup([f("hard")]).label).toBe("HARD");
  });

  test("link down: DISCONNECTED, whatever else is true", () => {
    const { container } = render(<App />);
    up();
    allFresh();
    act(() =>
      busSink.onStatus(
        "reconnecting",
        { attempt: 1, nextRetryAtMs: Date.now() + 1_000 },
        Date.now(),
      ),
    );
    expect(trustChip(container).getAttribute("data-state")).toBe(
      "disconnected",
    );
  });

  test("connected but silent: WAITING (never OK); every feed fresh and in limits: OK", () => {
    const { container } = render(<App />);
    up();
    expect(trustChip(container).getAttribute("data-state")).toBe("waiting");
    allFresh();
    expect(trustChip(container).getAttribute("data-state")).toBe("ok");
    expect(trustChip(container).textContent).toBe("OK");
  });

  test("a feed going quiet turns it STALE, and the tooltip names the feed", () => {
    const { container } = render(<App />);
    up();
    allFresh();
    tick(4_000);
    expect(trustChip(container).getAttribute("data-state")).toBe("stale");
    expect(
      container.querySelector(".bar-trust")?.getAttribute("title"),
    ).toContain("d1.health: stale");
  });

  test("limit levels: WARN then HARD from d1.risk.firm, and HARD names the Greek", () => {
    const { container } = render(<App />);
    up();
    allFresh(L.LIMIT_LEVEL_WARN);
    expect(trustChip(container).getAttribute("data-state")).toBe("warn");
    riskSnap(L.LIMIT_LEVEL_HARD);
    expect(trustChip(container).getAttribute("data-state")).toBe("hard");
    expect(container.querySelector(".bar-trust")?.getAttribute("title")).toBe(
      "AAPL vega HARD",
    );
  });

  test("a health fault is HARD worded FAULT, and a halted EXO book is HALTED", () => {
    const { container } = render(<App />);
    up();
    allFresh();
    health("d1Health", false);
    expect(trustChip(container).getAttribute("data-state")).toBe("hard");
    expect(trustChip(container).textContent).toBe("FAULT");
    health("d1Health");
    health("exoHealth", true, { "book.1.state": "halted:recon" });
    expect(trustChip(container).getAttribute("data-state")).toBe("halted");
    expect(
      container.querySelector(".bar-trust")?.getAttribute("title"),
    ).toContain("book 1 halted: recon");
  });
});
