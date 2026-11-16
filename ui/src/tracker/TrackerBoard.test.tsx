import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { SUBJECTS, keyOf } from "../bus/subjects";
import { TrackerAnalytics, TrackerAnalytics_Kind } from "../gen/live";
import { busSink, flushBus, resetBus } from "../store";
import { useNow } from "../useNow";
import { PRICE_ONLY_NOTE } from "./model";
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
const tracker = (id: string, book: number, symbol = "SPX", te = 3_820_000n) =>
  TrackerAnalytics.fromPartial({
    meta: { msgId: `${id}-${seq++}` }, // the store dedupes on msg_id across tests
    bookId: book,
    benchmark: { instrumentId: 9, symbol, instrumentClass: 2, currency: "USD" },
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
    send(tracker("b", 1, "NDX"));
    const cards = container.querySelectorAll(".tcard");
    expect(cards).toHaveLength(2);
    expect(cards[0]?.textContent).toContain("book 1"); // book_id order
    const c = (cards[1] as HTMLElement).textContent ?? "";
    expect(c).toContain("book 2 vs SPX");
    expect(c).toContain("ex-post");
    expect(c).toContain("38.2 bps");
    expect(c).toContain("−12.4 bps");
    expect(c).toContain("0.80 %");
    expect(c).toContain("n_obs 250");
    expect(c).toContain("sampling_interval_s 60");
    expect(c).toContain("window 4m00s");
    expect(c).toContain("DEMO");
    expect(c).toContain(PRICE_ONLY_NOTE);
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

  test("wire strings are literal text", () => {
    const evil = '<img src=x onerror="alert(1)"><script>x()</script>';
    const { container } = render(<Host />);
    up();
    send(tracker("a", 1, evil));
    expect(container.querySelector("img, script")).toBeNull();
    expect(container.querySelector(".tcard")?.textContent).toContain(evil);
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
