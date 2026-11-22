import { afterEach, describe, expect, test, vi } from "vitest";
import { SUBJECTS } from "./bus/subjects";
import { PositionSnapshot, TrackerAnalytics } from "./gen/live";
import { MsgIdLru, createBusStore, defaultSchedule } from "./store";

const pos = (msgId: string, bookId: number) =>
  PositionSnapshot.fromPartial({ meta: { msgId }, bookId });
const trk = (msgId: string, bookId: number) =>
  TrackerAnalytics.fromPartial({
    meta: { msgId },
    bookId,
    samplingIntervalS: 60,
  });

/** Manual scheduler: records the flush callback instead of running it. */
function manual() {
  const calls: (() => void)[] = [];
  return { schedule: (f: () => void) => calls.push(f), calls };
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("latest-wins and rings", () => {
  test("a keyed subject keeps the newest message per key", () => {
    const { store, sink, flush } = createBusStore({
      schedule: manual().schedule,
    });
    sink.onMessage("positions", "d1.positions.7", "7", pos("a", 7), 1000);
    sink.onMessage("positions", "d1.positions.8", "8", pos("b", 8), 1001);
    sink.onMessage("positions", "d1.positions.7", "7", pos("c", 7), 1002);
    flush();
    const s = store.getState().subjects.positions;
    expect(s.count).toBe(3);
    expect(Object.keys(s.latest).sort()).toEqual(["7", "8"]);
    expect(s.latest["7"]?.value.meta?.msgId).toBe("c");
    expect(s.last?.receivedAtMs).toBe(1002);
    expect(s.rings).toEqual({}); // ringSize 0
  });

  test("a keyed ring is bounded per key, oldest dropped first", () => {
    const { store, sink, flush } = createBusStore({
      schedule: manual().schedule,
    });
    const cap = SUBJECTS.tracker.ringSize;
    for (let i = 0; i < cap + 10; i++)
      sink.onMessage("tracker", "d1.tracker.1", "1", trk(`t${i}`, 1), i);
    sink.onMessage("tracker", "d1.tracker.2", "2", trk("other", 2), 0);
    flush();
    const rings = store.getState().subjects.tracker.rings;
    expect(rings["1"]).toHaveLength(cap);
    expect(rings["1"]?.[0]?.value.meta?.msgId).toBe("t10");
    expect(rings["1"]?.[cap - 1]?.value.meta?.msgId).toBe(`t${cap + 9}`);
    expect(rings["2"]).toHaveLength(1);
  });

  test("a subject-wide ring spans keys and is bounded across flushes", () => {
    const { store, sink, flush } = createBusStore({
      schedule: manual().schedule,
    });
    const cap = SUBJECTS.limitAlerts.ringSize;
    for (let i = 0; i < cap + 5; i++) {
      sink.onMessage("limitAlerts", "d1.alerts.limits", "", pos(`a${i}`, 0), i);
      if (i % 50 === 0) flush();
    }
    flush();
    const ring = store.getState().subjects.limitAlerts.rings[""];
    expect(ring).toHaveLength(cap);
    expect(ring?.[0]?.value.meta?.msgId).toBe("a5");
  });

  test("a flush never mutates the previous state (untouched subjects keep their reference)", () => {
    const { store, sink, flush } = createBusStore({
      schedule: manual().schedule,
    });
    sink.onMessage("tracker", "d1.tracker.1", "1", trk("t0", 1), 1);
    flush();
    const before = store.getState();
    const ringBefore = before.subjects.tracker.rings["1"];
    sink.onMessage("tracker", "d1.tracker.1", "1", trk("t1", 1), 2);
    flush();
    const after = store.getState();
    expect(ringBefore).toHaveLength(1);
    expect(after.subjects.tracker.rings["1"]).toHaveLength(2);
    expect(after.subjects.positions).toBe(before.subjects.positions);
  });
});

describe("batching", () => {
  test("many messages in a frame: one scheduled flush, one state update", () => {
    const m = manual();
    const { store, sink } = createBusStore({ schedule: m.schedule });
    let updates = 0;
    store.subscribe(() => (updates += 1));
    for (let i = 0; i < 100; i++)
      sink.onMessage("positions", "d1.positions.7", "7", pos(`p${i}`, 7), i);
    expect(m.calls).toHaveLength(1);
    expect(store.getState().subjects.positions.count).toBe(0); // nothing visible before the flush
    expect(updates).toBe(0);
    m.calls[0]?.();
    expect(updates).toBe(1);
    expect(store.getState().subjects.positions.count).toBe(100);
    sink.onMessage("positions", "d1.positions.7", "7", pos("next", 7), 200);
    expect(m.calls).toHaveLength(2); // a new frame schedules again
  });

  test("without requestAnimationFrame the fallback is a 250 ms timer", () => {
    vi.useFakeTimers();
    vi.stubGlobal("requestAnimationFrame", undefined);
    const flush = vi.fn();
    defaultSchedule(flush);
    vi.advanceTimersByTime(249);
    expect(flush).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(flush).toHaveBeenCalledTimes(1);
  });

  test("with requestAnimationFrame it flushes on the frame, once, and drops the safety timer", () => {
    vi.useFakeTimers();
    let frame: (() => void) | undefined;
    vi.stubGlobal("requestAnimationFrame", (cb: () => void) => (frame = cb));
    const flush = vi.fn();
    defaultSchedule(flush);
    frame?.();
    vi.advanceTimersByTime(5_000);
    expect(flush).toHaveBeenCalledTimes(1);
  });

  test("a hidden tab (rAF never fires) still drains on the safety timer", () => {
    vi.useFakeTimers();
    vi.stubGlobal("requestAnimationFrame", () => 0);
    const flush = vi.fn();
    defaultSchedule(flush);
    vi.advanceTimersByTime(1_000);
    expect(flush).toHaveBeenCalledTimes(1);
  });
});

describe("msg_id dedupe", () => {
  test("a redelivered message is dropped and counted", () => {
    const { store, sink, flush } = createBusStore({
      schedule: manual().schedule,
    });
    sink.onMessage("positions", "d1.positions.7", "7", pos("same", 7), 1);
    sink.onMessage("positions", "d1.positions.7", "7", pos("same", 7), 2);
    flush();
    expect(store.getState().subjects.positions.count).toBe(1);
    expect(store.getState().duplicates).toBe(1);
  });

  test("an empty msg_id is never deduped", () => {
    const { store, sink, flush } = createBusStore({
      schedule: manual().schedule,
    });
    sink.onMessage("positions", "d1.positions.7", "7", pos("", 7), 1);
    sink.onMessage("positions", "d1.positions.7", "7", pos("", 7), 2);
    flush();
    expect(store.getState().subjects.positions.count).toBe(2);
  });

  test("the LRU is bounded: the oldest id is forgotten, a re-seen id is refreshed", () => {
    const lru = new MsgIdLru(2);
    expect(lru.seen("a")).toBe(false);
    expect(lru.seen("b")).toBe(false);
    expect(lru.seen("a")).toBe(true); // refresh a; b is now oldest
    expect(lru.seen("c")).toBe(false); // evicts b
    expect(lru.size).toBe(2);
    expect(lru.seen("a")).toBe(true);
    expect(lru.seen("b")).toBe(false); // forgotten
  });

  test("the store's LRU stays at its cap", () => {
    const { store, sink, flush } = createBusStore({
      schedule: manual().schedule,
      lruSize: 3,
    });
    for (let i = 0; i < 10; i++)
      sink.onMessage("positions", "d1.positions.7", "7", pos(`m${i}`, 7), i);
    sink.onMessage("positions", "d1.positions.7", "7", pos("m0", 7), 11); // evicted long ago: accepted again
    flush();
    expect(store.getState().subjects.positions.count).toBe(11);
    expect(store.getState().duplicates).toBe(0);
  });
});
