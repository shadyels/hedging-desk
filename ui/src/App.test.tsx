import { act, cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { App } from "./App";
import { isEventSubject } from "./bus/freshness";
import { SUBJECTS } from "./bus/subjects";
import { StateChip, CHIP_LABEL, type ChipState } from "./components/StateChip";
import { HealthStatus } from "./gen/live";
import { busSink, flushBus, resetBus } from "./store";

const T0 = Date.UTC(2026, 10, 8, 12, 4, 9);

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(T0);
  resetBus();
});
afterEach(() => {
  cleanup(); // vitest has no globals, so RTL does not auto-clean
  vi.useRealTimers();
});

const up = () =>
  busSink.onStatus(
    "connected",
    { attempt: 0, nextRetryAtMs: null },
    Date.now(),
  );
const tick = (ms: number) => act(() => void vi.advanceTimersByTime(ms));
const health = (id: string, component: string, ok = true) =>
  HealthStatus.fromPartial({ meta: { msgId: id }, component, ok });
function sendHealth(id: string, component: string, ok = true) {
  act(() => {
    busSink.onMessage(
      "d1Health",
      "d1.health",
      "",
      health(id, component, ok),
      Date.now(),
    );
    flushBus();
  });
}
const row = (pattern: string) => {
  // Scoped to the subjects table: the health screen also names these subjects in its panel headers.
  const th = within(screen.getByRole("table", { name: "Bus subjects" }))
    .getByText(pattern, { selector: "code" })
    .closest("tr");
  if (!th) throw new Error(`no row for ${pattern}`);
  return within(th);
};

describe("shell", () => {
  test("lists every registry subject", () => {
    render(<App />);
    for (const spec of Object.values(SUBJECTS)) {
      expect(
        screen.getAllByText(spec.pattern, { selector: "code" }).length,
      ).toBeGreaterThan(0);
    }
  });

  test("F4: first load reads CONNECTING, never DISCONNECTED and never 'since –'", () => {
    const { container } = render(<App />);
    act(() =>
      busSink.onStatus(
        "connecting",
        { attempt: 0, nextRetryAtMs: null },
        Date.now(),
      ),
    );
    const banner = screen.getByRole("status").textContent ?? "";
    expect(banner).toContain("CONNECTING");
    expect(banner).not.toContain("DISCONNECTED");
    expect(banner).not.toMatch(/since/);
    expect(banner).not.toContain("–");
    expect(container.querySelector(".bar-conn")?.textContent).toContain(
      "CONNECTING",
    );
    expect(
      container.querySelector("main")?.getAttribute("data-disconnected"),
    ).toBe("true");
    expect(container.querySelector('.chip[data-state="ok"]')).toBeNull();
  });

  test("F4: a lost link (never a first dial) is DISCONNECTED with its time, and the first failed dial too", () => {
    render(<App />);
    act(() =>
      busSink.onStatus(
        "reconnecting",
        { attempt: 1, nextRetryAtMs: Date.now() + 1_000 },
        Date.now(),
      ),
    );
    const banner = screen.getByRole("status").textContent ?? "";
    expect(banner).toContain("DISCONNECTED since 12:04:09Z");
    expect(banner).not.toContain("CONNECTING");
    expect(banner).toContain("no data received");
  });

  test("connected: banner gone, every cadence subject WAITING (never received), event subjects 'none since connect', none STALE", () => {
    const { container } = render(<App />);
    act(up);
    expect(screen.queryByRole("status")).toBeNull();
    expect(
      container.querySelector("main")?.getAttribute("data-disconnected"),
    ).toBe("false");
    const events = Object.values(SUBJECTS).filter(isEventSubject);
    expect(events.length).toBe(5); // alerts, recon, exec, crosses, targets
    expect(
      container.querySelectorAll('.subjects .chip[data-state="waiting"]')
        .length,
    ).toBe(Object.keys(SUBJECTS).length - events.length);
    expect(container.querySelectorAll(".subjects .none-since")).toHaveLength(
      events.length,
    );
    expect(
      row("d1.alerts.limits").getByText(/none since connect 12:04:09Z/),
    ).toBeDefined();
    expect(container.querySelector('.chip[data-state="stale"]')).toBeNull();
  });

  test("waiting shows a clock that ticks locally", () => {
    render(<App />);
    act(up);
    tick(65_000);
    expect(row("d1.health").getByText(/WAITING 1m05s/)).toBeDefined();
  });

  test("ok, then stale with its age, while a silent subject stays waiting", () => {
    render(<App />);
    act(up);
    sendHealth("h1", "delta-one");
    expect(row("d1.health").getByText(/^OK/)).toBeDefined();
    tick(4_000);
    expect(row("d1.health").getByText(/STALE 4s/)).toBeDefined();
    expect(row("exo.health").getByText(/WAITING/)).toBeDefined();
  });

  test("a health fault uses the HARD cue with the word FAULT", () => {
    const { container } = render(<App />);
    act(up);
    sendHealth("h2", "delta-one", false);
    expect(row("d1.health").getByText(/FAULT/)).toBeDefined();
    expect(container.querySelector('.chip[data-state="hard"]')).not.toBeNull();
  });

  test("link lost: banner with last-data age and retry countdown, board greyed", () => {
    const { container } = render(<App />);
    act(up);
    sendHealth("h3", "delta-one");
    tick(22_000);
    act(() =>
      busSink.onStatus(
        "reconnecting",
        { attempt: 2, nextRetryAtMs: Date.now() + 1_000 },
        Date.now(),
      ),
    );
    const banner = screen.getByRole("status").textContent ?? "";
    expect(banner).toContain("DISCONNECTED since 12:04:31Z");
    expect(banner).toContain("last data 22s old");
    expect(banner).toContain("attempt 2, retry in 1s");
    expect(
      container.querySelector("main")?.getAttribute("data-disconnected"),
    ).toBe("true");
  });
});

describe("state chip", () => {
  test("every state of the DESIGN.md vocabulary renders its label and data-state", () => {
    const states = Object.keys(CHIP_LABEL) as ChipState[];
    expect(states).toHaveLength(7);
    const { container } = render(
      <>
        {states.map((s) => (
          <StateChip key={s} state={s} />
        ))}
      </>,
    );
    for (const s of states) {
      const chip = container.querySelector(`.chip[data-state="${s}"]`);
      expect(chip?.textContent).toBe(CHIP_LABEL[s]);
    }
  });
});

describe("wire text is text", () => {
  const payload = "<img src=x onerror=\"document.title='pwned'\">";

  test("a hostile wire string renders as literal text, no element is created", () => {
    const { container } = render(<App />);
    act(up);
    sendHealth("x1", payload);
    expect(container.querySelector("img")).toBeNull();
    const cell = container.querySelector(".subjects .wire-text");
    expect(cell?.textContent).toBe(`${payload}: ok`);
    expect(cell?.getAttribute("title")).toBe(`${payload}: ok`);
    expect(cell?.children.length).toBe(0);
    expect(document.title).not.toBe("pwned");
  });
});
