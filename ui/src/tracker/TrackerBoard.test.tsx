import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { SUBJECTS, keyOf } from "../bus/subjects";
import { TrackerAnalytics, TrackerAnalytics_Kind } from "../gen/live";
import { busSink, flushBus, resetBus } from "../store";
import { ink, loadCss, token } from "../testCss";
import { useNow } from "../useNow";
import { PRICE_ONLY_NOTE, PRICE_ONLY_SHORT } from "./model";
import { TrackerBoard } from "./TrackerBoard";

let seq = 0;
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

function Host() {
  return <TrackerBoard now={useNow()} />;
}
const up = () =>
  act(() =>
    busSink.onStatus(
      "connected",
      { attempt: 0, nextRetryAtMs: null },
      Date.now(),
    ),
  );
const tick = (ms: number) => act(() => void vi.advanceTimersByTime(ms));
// D1 puts only the id on the benchmark InstrumentRef (2001 = SPX in universe.json); `symbol` is a decoy.
const tracker = (
  id: string,
  book: number,
  benchId = 2001,
  symbol = "",
  te = 3_820_000n,
) =>
  TrackerAnalytics.fromPartial({
    meta: { msgId: `${id}-${seq++}` }, // the store dedupes on msg_id across tests
    bookId: book,
    benchmark: {
      instrumentId: benchId,
      symbol,
      instrumentClass: 2,
      currency: "USD",
    },
    kind: TrackerAnalytics_Kind.KIND_EX_POST,
    trackingErrorAnnE9: te,
    trackingDiffE9: -1_240_000n,
    cashWeightE9: 8_000_000n,
    cashDragE9: -310_000n,
    nObs: 250,
    windowStartNs: 1_000_000_000n,
    windowEndNs: 241_000_000_000n,
    samplingIntervalS: 60,
  });
const send = (m: TrackerAnalytics, book = m.bookId) =>
  act(() => {
    const subject = `d1.tracker.${book}`;
    busSink.onMessage(
      "tracker",
      subject,
      keyOf(SUBJECTS.tracker.pattern, subject),
      m,
      Date.now(),
    );
    flushBus();
  });

describe("tracker scorecard", () => {
  test("waiting: no message, no number, not stale", () => {
    const { container } = render(<Host />);
    up();
    tick(300_000);
    expect(
      container.querySelector('.chip[data-state="waiting"]'),
    ).not.toBeNull();
    expect(container.querySelector('.chip[data-state="stale"]')).toBeNull();
    expect(container.querySelector(".tcard")).toBeNull();
    expect(container.textContent).toContain("Waiting for the first");
  });

  test("a card renders the proto fields, DEMO badge and price-only note", () => {
    const { container } = render(<Host />);
    up();
    send(tracker("a", 2));
    send(tracker("b", 1));
    const cards = container.querySelectorAll(".tcard");
    expect(cards).toHaveLength(2);
    expect(cards[0]?.textContent).toContain("book 1"); // book_id order
    const c = (cards[1] as HTMLElement).textContent ?? "";
    expect(c).toContain("book 2 vs SPX");
    expect(c).toContain("ex-post");
    expect(c).toContain("38.2 bps");
    expect(c).toContain("−12.4 bps");
    expect(c).toContain("0.80 %");
    expect(c).toContain("250 obs");
    expect(c).toContain("every 60 s");
    expect(cards[1]?.querySelector('[title="n_obs"]')).not.toBeNull();
    expect(
      cards[1]?.querySelector('[title="sampling_interval_s"]'),
    ).not.toBeNull();
    expect(c).toContain("window 4m00s");
    expect(c).toContain("DEMO");
    expect(c).toContain(PRICE_ONLY_SHORT);
    expect(c).not.toContain("ADR-010"); // the citation is a tooltip
    expect(cards[1]?.querySelector(".tnote")?.getAttribute("title")).toBe(
      PRICE_ONLY_NOTE,
    );
    expect(c).toContain("updated 0s ago");
    expect(cards[1]?.querySelector("svg[role=img]")).not.toBeNull();
  });

  test("stale card shows its age and keeps the values", () => {
    const { container } = render(<Host />);
    up();
    send(tracker("a", 1));
    tick(150_000);
    expect(container.querySelector('.chip[data-state="stale"]')).toBeNull();
    tick(1_000);
    const card = container.querySelector(".tcard") as HTMLElement;
    expect(card.getAttribute("data-fresh")).toBe("stale");
    expect(card.textContent).toContain("STALE 2m31s");
    expect(card.textContent).toContain("38.2 bps");
    expect(card.textContent).toContain("last message 12:04:09Z");
  });

  test("the title names the benchmark from reference data: book 1 reads SPX", () => {
    const { container } = render(<Host />);
    up();
    send(tracker("a", 1));
    expect(container.querySelector(".tcard h3")?.textContent).toContain(
      "book 1 vs SPX",
    );
    // the id stays auditable in a tooltip
    expect(
      container.querySelector('.tcard [title="instrument_id 2001"]'),
    ).not.toBeNull();
  });

  test("a benchmark with no id falls back to the book's benchmark in reference data; an unknown id is labelled", () => {
    const { container } = render(<Host />);
    up();
    send(tracker("a", 1, 0));
    send(tracker("b", 2, 9));
    send(tracker("c", 3, 0));
    const cards = container.querySelectorAll(".tcard");
    expect(cards[0]?.textContent).toContain("book 1 vs SPX");
    expect(cards[1]?.textContent).toContain(
      "book 2 vs id 9 (not in reference data)",
    );
    expect(cards[2]?.textContent).toContain(
      "book 3 vs no benchmark in reference data",
    );
  });

  test("a symbol on the wire is never rendered, as markup or as text", () => {
    const evil = '<img src=x onerror="alert(1)"><script>x()</script>';
    const { container } = render(<Host />);
    up();
    send(tracker("a", 1, 2001, evil));
    expect(container.querySelector("img, script")).toBeNull();
    const text = container.querySelector(".tcard")?.textContent ?? "";
    expect(text).toContain("book 1 vs SPX");
    expect(text).not.toContain(evil);
  });

  test("no limit state class or colour token on any tracker value", () => {
    const { container } = render(<Host />);
    up();
    send(tracker("a", 1));
    send(tracker("b", 2));
    tick(200_000); // one stale rendering too
    const card = container.querySelector(".tcard") as HTMLElement;
    const html = card.innerHTML;
    expect(html).not.toMatch(
      /--state-|data-level|data-state="(ok|warn|hard)"|hbar/,
    );
    // Only the freshness chip may carry a state; it is never inside a value element.
    for (const v of Array.from(card.querySelectorAll(".tval, .tval *"))) {
      expect(v.closest(".chip")).toBeNull();
      expect(v.getAttribute("data-level")).toBeNull();
      expect(v.getAttribute("style")).toBeNull();
    }
  });
});

describe("tracker card presentation", () => {
  test("F5: a tracker value follows the board's disconnected dimming, it does not pin --text", () => {
    const off = loadCss("tracker", "shell");
    try {
      const { container } = render(
        <main className="board" data-disconnected="true">
          <Host />
        </main>,
      );
      up();
      send(tracker("a", 1));
      const dim = (el: Element | null) => ink(el);
      const val = container.querySelector(".tval");
      const kpi = container.querySelector(".tval.kpi");
      expect(dim(val)).toBe("var(--text-3)");
      expect(dim(kpi)).toBe("var(--text-3)");
      // and the rule itself does not name --text
      const rule = Array.from(document.styleSheets)
        .flatMap((s) => Array.from(s.cssRules))
        .find((r) => r instanceof CSSStyleRule && r.selectorText === ".tval");
      expect(rule).toBeDefined();
      expect((rule as CSSStyleRule).style.color).not.toBe("var(--text)");
    } finally {
      off();
    }
  });

  test("F8: the KPI figure is weight 600 and ordinary values are not", () => {
    const off = loadCss("tracker");
    try {
      const { container } = render(<Host />);
      up();
      send(tracker("a", 1));
      const kpi = container.querySelector(".tval.kpi") as Element;
      const plain = container.querySelectorAll(".tval:not(.kpi)")[0] as Element;
      expect(getComputedStyle(kpi).fontWeight).toBe("var(--fw-bold)");
      expect(token("--fw-bold")).toBe("600");
      expect(getComputedStyle(plain).fontWeight).not.toBe("var(--fw-bold)");
    } finally {
      off();
    }
  });

  test("F9: a stale card carries a readable stale cue and its age at the top, above the values", () => {
    const { container } = render(<Host />);
    up();
    send(tracker("a", 1));
    expect(container.querySelector(".tstale")).toBeNull(); // live card: no strip
    tick(200_000);
    const card = container.querySelector(".tcard") as HTMLElement;
    const strip = card.querySelector(".tstale") as HTMLElement;
    expect(strip.textContent).toContain("STALE 3m20s");
    expect(strip.textContent).toContain("last known values");
    expect(strip.textContent).toContain("last message 12:04:09Z");
    // first thing after the title row, before any value
    expect(strip.nextElementSibling?.className).toBe("tlist");
    expect(
      card.querySelector('.chip[data-state="stale"]')?.textContent,
    ).toContain("STALE 3m20s");
  });
});
