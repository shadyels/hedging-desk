import { describe, expect, test, vi, afterEach } from "vitest";
import { freshness } from "../bus/freshness";
import { SUBJECTS } from "../bus/subjects";
import { TrackerAnalytics } from "../gen/live";
import { initialState } from "../store";
import { sparkPoints, windowSpanMs, type SparkSample } from "./model";

afterEach(() => vi.useRealTimers());

const msg = (interval: number) =>
  TrackerAnalytics.fromPartial({
    meta: { msgId: "t" },
    bookId: 1,
    samplingIntervalS: interval,
  });
const T0 = Date.UTC(2026, 10, 8, 12, 0, 0);
const conn = (up = true) => ({
  ...initialState().conn,
  status: up ? ("connected" as const) : ("reconnecting" as const),
  firstConnectedAtMs: T0,
  connectedAtMs: T0,
});

describe("tracker stale threshold = 2.5 x sampling_interval_s", () => {
  const entry = { value: msg(60), receivedAtMs: T0 };
  const at = (ms: number) => {
    vi.useFakeTimers();
    vi.setSystemTime(T0 + ms);
    return freshness(SUBJECTS.tracker, entry, conn(), Date.now()).state;
  };
  test("just before and exactly at 150 s: ok", () => {
    expect(at(149_999)).toBe("ok");
    expect(at(150_000)).toBe("ok");
  });
  test("just after: stale", () => {
    expect(at(150_001)).toBe("stale");
  });
  test("waiting (never received) is not stale, however long", () => {
    vi.useFakeTimers();
    vi.setSystemTime(T0 + 3_600_000);
    const f = freshness(SUBJECTS.tracker, null, conn(), Date.now());
    expect(f.state).toBe("waiting");
  });
  test("threshold follows the message's own interval", () => {
    const e = { value: msg(10), receivedAtMs: T0 };
    expect(freshness(SUBJECTS.tracker, e, conn(), T0 + 25_000).state).toBe(
      "ok",
    );
    expect(freshness(SUBJECTS.tracker, e, conn(), T0 + 25_001).state).toBe(
      "stale",
    );
  });
});

describe("sparkPoints", () => {
  const finite = (p: { x: number; y: number }[]) =>
    p.every((q) => Number.isFinite(q.x) && Number.isFinite(q.y));
  test("empty ring: no points", () => {
    expect(sparkPoints([], 200, 28)).toEqual([]);
  });
  test("one point: finite, at the right edge", () => {
    const p = sparkPoints([{ t: 5, v: 7n }], 200, 28);
    expect(p).toHaveLength(1);
    expect(finite(p)).toBe(true);
    expect(p[0]?.x).toBe(199);
  });
  test("flat series: finite, all on the middle line", () => {
    const s: SparkSample[] = [0, 1, 2].map((t) => ({ t, v: 3n }));
    const p = sparkPoints(s, 200, 28);
    expect(finite(p)).toBe(true);
    expect(p.every((q) => q.y === 14)).toBe(true);
  });
  test("same timestamps: finite", () => {
    const p = sparkPoints(
      [
        { t: 1, v: 1n },
        { t: 1, v: 2n },
      ],
      200,
      28,
    );
    expect(finite(p)).toBe(true);
  });
  test("x is linear in time, min at the bottom, max at the top", () => {
    const p = sparkPoints(
      [
        { t: 0, v: 0n },
        { t: 10, v: 5n },
        { t: 40, v: 10n },
      ],
      101,
      11,
      0,
    );
    expect(p.map((q) => q.x)).toEqual([0, 25.25, 101]);
    expect(p.map((q) => q.y)).toEqual([11, 5.5, 0]);
  });
  test("i64-sized values do not lose precision or go NaN", () => {
    const p = sparkPoints(
      [
        { t: 0, v: -(2n ** 62n) },
        { t: 1, v: 2n ** 62n },
      ],
      100,
      20,
    );
    expect(finite(p)).toBe(true);
    expect(p[0]?.y).toBe(19);
    expect(p[1]?.y).toBe(1);
  });
});

describe("windowSpanMs", () => {
  test("feed-clock difference in ms; unusable input is null", () => {
    expect(
      windowSpanMs(
        TrackerAnalytics.fromPartial({
          windowStartNs: 1_000_000_000n,
          windowEndNs: 4_000_000_000n,
        }),
      ),
    ).toBe(3000);
    expect(windowSpanMs(TrackerAnalytics.fromPartial({}))).toBeNull();
  });
});
