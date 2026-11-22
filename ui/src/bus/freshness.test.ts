import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import {
  HealthStatus,
  PositionSnapshot,
  TrackerAnalytics,
  ExecutionReport,
  MarkPrice,
} from "../gen/live";
import { createBusStore } from "../store";
import { freshness } from "./freshness";
import { SUBJECTS, type SubjectId, type WithMeta } from "./subjects";

const T0 = Date.UTC(2026, 10, 8, 12, 0, 0);

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(T0);
});
afterEach(() => vi.useRealTimers());

function harness() {
  const { store, sink, flush } = createBusStore({ schedule: (f) => f() });
  const up = () =>
    sink.onStatus("connected", { attempt: 0, nextRetryAtMs: null }, Date.now());
  const down = () =>
    sink.onStatus(
      "reconnecting",
      { attempt: 1, nextRetryAtMs: null },
      Date.now(),
    );
  const send = (id: SubjectId, subject: string, key: string, msg: WithMeta) =>
    sink.onMessage(id, subject, key, msg, Date.now());
  const read = (id: SubjectId) => {
    const s = store.getState();
    return freshness(SUBJECTS[id], s.subjects[id].last, s.conn, Date.now());
  };
  return { store, sink, flush, up, down, send, read };
}

let n = 0;
const health = () =>
  HealthStatus.fromPartial({
    meta: { msgId: `h${n++}` },
    component: "d1",
    ok: true,
  });

describe("waiting vs stale vs ok", () => {
  test("never disconnected-and-ok before the link is up: everything reads disconnected", () => {
    const h = harness();
    expect(h.read("d1Health").state).toBe("disconnected");
  });

  test("connected with no message: waiting, with a clock; it never escalates to stale", () => {
    const h = harness();
    h.up();
    expect(h.read("d1Health")).toEqual({ state: "waiting", ageMs: 0 });
    vi.advanceTimersByTime(65_000);
    expect(h.read("d1Health")).toEqual({ state: "waiting", ageMs: 65_000 });
    vi.advanceTimersByTime(3_600_000);
    expect(h.read("d1Health").state).toBe("waiting");
  });

  test("a message makes it ok; older than 3 s makes it stale (boundary: > threshold); a fresh one clears it", () => {
    const h = harness();
    h.up();
    h.send("d1Health", "d1.health", "", health());
    expect(h.read("d1Health")).toEqual({ state: "ok", ageMs: 0 });
    vi.advanceTimersByTime(3_000);
    expect(h.read("d1Health").state).toBe("ok");
    vi.advanceTimersByTime(1);
    expect(h.read("d1Health")).toEqual({ state: "stale", ageMs: 3_001 });
    h.send("d1Health", "d1.health", "", health());
    expect(h.read("d1Health").state).toBe("ok");
  });

  test("waiting and stale are independent per subject", () => {
    const h = harness();
    h.up();
    h.send("d1Health", "d1.health", "", health());
    vi.advanceTimersByTime(5_000);
    expect(h.read("d1Health").state).toBe("stale");
    expect(h.read("exoHealth").state).toBe("waiting");
  });

  test("valuations go stale at 12 s", () => {
    const h = harness();
    h.up();
    h.send(
      "valuations",
      "exo.valuations.1",
      "1",
      PositionSnapshot.fromPartial({ meta: { msgId: "v1" } }),
    );
    vi.advanceTimersByTime(12_000);
    expect(h.read("valuations").state).toBe("ok");
    vi.advanceTimersByTime(1);
    expect(h.read("valuations").state).toBe("stale");
  });

  test("tracker threshold comes from the message: 2.5 x samplingIntervalS", () => {
    const h = harness();
    h.up();
    h.send(
      "tracker",
      "d1.tracker.1",
      "1",
      TrackerAnalytics.fromPartial({
        meta: { msgId: "k1" },
        samplingIntervalS: 60,
      }),
    );
    vi.advanceTimersByTime(150_000);
    expect(h.read("tracker").state).toBe("ok");
    vi.advanceTimersByTime(1);
    expect(h.read("tracker").state).toBe("stale");
  });

  test("event-driven subjects show an age and are never stale", () => {
    const h = harness();
    h.up();
    h.send(
      "exec",
      "d1.exec.0.1",
      "0.1",
      ExecutionReport.fromPartial({ meta: { msgId: "e1" } }),
    );
    vi.advanceTimersByTime(24 * 3_600_000);
    expect(h.read("exec")).toEqual({ state: "ok", ageMs: 24 * 3_600_000 });
  });

  test("an undecided threshold (marks) gives an age but no stale verdict", () => {
    const h = harness();
    h.up();
    h.send(
      "marks",
      "d1.marks.1",
      "1",
      MarkPrice.fromPartial({ meta: { msgId: "mk1" } }),
    );
    vi.advanceTimersByTime(600_000);
    expect(h.read("marks").state).toBe("ok");
  });
});

describe("disconnect and reconnect", () => {
  test("link down: disconnected for every subject, last-data age kept", () => {
    const h = harness();
    h.up();
    h.send("d1Health", "d1.health", "", health());
    vi.advanceTimersByTime(1_000);
    h.down();
    expect(h.read("d1Health")).toEqual({ state: "disconnected", ageMs: 1_000 });
    expect(h.read("exoHealth").state).toBe("disconnected");
  });

  test("after reconnect, previously received values read stale until a fresh message; never-reported stays waiting", () => {
    const h = harness();
    h.up();
    h.send("d1Health", "d1.health", "", health());
    h.down();
    vi.advanceTimersByTime(500);
    h.up(); // 0.5 s old: inside the 3 s threshold, yet it predates the reconnect
    expect(h.read("d1Health").state).toBe("stale");
    expect(h.read("exoHealth").state).toBe("waiting");
    h.send("d1Health", "d1.health", "", health());
    expect(h.read("d1Health").state).toBe("ok");
  });

  test("event-driven subjects do not turn stale after a reconnect", () => {
    const h = harness();
    h.up();
    h.send(
      "exec",
      "d1.exec.0.1",
      "0.1",
      ExecutionReport.fromPartial({ meta: { msgId: "e9" } }),
    );
    h.down();
    vi.advanceTimersByTime(500);
    h.up();
    expect(h.read("exec").state).toBe("ok");
  });
});
