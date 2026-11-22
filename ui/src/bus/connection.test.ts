import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { PositionSnapshot } from "../gen/live";
import { createBusStore } from "../store";
import {
  backoffMs,
  startConnection,
  type BusConnection,
  type Connector,
} from "./connection";
import { SUBJECTS, SUBJECT_IDS } from "./subjects";

/** A scripted stand-in for the nats-core layer. The store under test is the real one. */
class FakeConn implements BusConnection {
  readonly subs = new Map<
    string,
    (subject: string, data: Uint8Array) => void
  >();
  private lost!: () => void;
  private readonly gone = new Promise<void>((r) => (this.lost = r));
  closeCalls = 0;
  subscribe(pattern: string, cb: (subject: string, data: Uint8Array) => void) {
    this.subs.set(pattern, cb);
  }
  closed() {
    return this.gone;
  }
  async close() {
    this.closeCalls += 1;
    this.lost();
  }
  drop() {
    this.lost();
  }
  emit(pattern: string, subject: string, data: Uint8Array) {
    const cb = this.subs.get(pattern);
    if (!cb) throw new Error(`not subscribed: ${pattern}`);
    cb(subject, data);
  }
}

function scripted(outcomes: ("ok" | "fail")[]) {
  const conns: FakeConn[] = [];
  let dials = 0;
  const connect: Connector = async () => {
    const outcome = outcomes[dials] ?? "ok";
    dials += 1;
    if (outcome === "fail") throw new Error("dial failed");
    const c = new FakeConn();
    conns.push(c);
    return c;
  };
  return { connect, conns, dials: () => dials };
}

const T0 = Date.UTC(2026, 10, 8, 12, 0, 0);
const patterns = SUBJECT_IDS.map((id) => SUBJECTS[id].pattern).sort();

function setup(outcomes: ("ok" | "fail")[]) {
  const { store, sink } = createBusStore({ schedule: (flush) => flush() });
  const log = vi.fn();
  const script = scripted(outcomes);
  const handle = startConnection({ connect: script.connect, sink, log });
  return { store, script, log, handle };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(T0);
});
afterEach(() => {
  vi.useRealTimers();
});

describe("backoff", () => {
  test("doubles from 500 ms and caps at 15 s", () => {
    expect([1, 2, 3, 4, 5, 6, 7, 8].map(backoffMs)).toEqual([
      500, 1000, 2000, 4000, 8000, 15000, 15000, 15000,
    ]);
  });
});

describe("connection manager", () => {
  test("connects, subscribes to every registry subject, reports connected", async () => {
    const { store, script } = setup(["ok"]);
    await vi.advanceTimersByTimeAsync(0);
    expect(store.getState().conn.status).toBe("connected");
    expect([...(script.conns[0]?.subs.keys() ?? [])].sort()).toEqual(patterns);
    expect(store.getState().conn.firstConnectedAtMs).toBe(T0);
  });

  test("link loss: reconnecting with the backoff delay, then resubscribes on the NEW connection", async () => {
    const { store, script } = setup(["ok"]);
    await vi.advanceTimersByTimeAsync(0);
    script.conns[0]?.drop();
    await vi.advanceTimersByTimeAsync(0);
    let { conn } = store.getState();
    expect(conn.status).toBe("reconnecting");
    expect(conn.attempt).toBe(1);
    expect(conn.nextRetryAtMs).toBe(T0 + 500);
    expect(conn.disconnectedSinceMs).toBe(T0);

    await vi.advanceTimersByTimeAsync(499);
    expect(script.dials()).toBe(1); // not yet
    await vi.advanceTimersByTimeAsync(1);
    expect(script.dials()).toBe(2);
    expect([...(script.conns[1]?.subs.keys() ?? [])].sort()).toEqual(patterns);
    ({ conn } = store.getState());
    expect(conn.status).toBe("connected");
    expect(conn.attempt).toBe(0);
    expect(conn.disconnectedSinceMs).toBeNull();
    expect(conn.connectedAtMs).toBe(T0 + 500);
    expect(conn.firstConnectedAtMs).toBe(T0); // the WAITING clock does not restart
  });

  test("failed dials back off 0.5 s, 1 s, 2 s; a success resets the ladder", async () => {
    const { store, script } = setup(["fail", "fail", "fail", "ok", "ok"]);
    await vi.advanceTimersByTimeAsync(0);
    expect(script.dials()).toBe(1);
    expect(store.getState().conn.attempt).toBe(1);

    await vi.advanceTimersByTimeAsync(500); // 2nd dial
    expect(script.dials()).toBe(2);
    expect(store.getState().conn.attempt).toBe(2);
    expect(store.getState().conn.nextRetryAtMs).toBe(T0 + 500 + 1000);

    await vi.advanceTimersByTimeAsync(999);
    expect(script.dials()).toBe(2);
    await vi.advanceTimersByTimeAsync(1); // 3rd dial
    expect(script.dials()).toBe(3);

    await vi.advanceTimersByTimeAsync(2000); // 4th dial succeeds
    expect(script.dials()).toBe(4);
    expect(store.getState().conn.status).toBe("connected");

    script.conns[0]?.drop();
    await vi.advanceTimersByTimeAsync(0);
    expect(store.getState().conn.attempt).toBe(1); // reset: back to 500 ms, not 4 s
    await vi.advanceTimersByTimeAsync(500);
    expect(script.dials()).toBe(5);
  });

  test("a bad payload is counted and logged, does not throw, and the next good one still lands", async () => {
    const { store, script, log } = setup(["ok"]);
    await vi.advanceTimersByTimeAsync(0);
    const conn = script.conns[0];
    expect(conn).toBeDefined();
    const garbage = new Uint8Array([0xff, 0xff, 0xff, 0xff]);
    expect(() =>
      conn?.emit("d1.positions.*", "d1.positions.7", garbage),
    ).not.toThrow();
    expect(() =>
      conn?.emit("d1.positions.*", "d1.positions.7", garbage),
    ).not.toThrow();
    expect(store.getState().subjects.positions.decodeErrors).toBe(2);
    expect(store.getState().subjects.positions.count).toBe(0); // still WAITING: garbage is not data
    expect(log).toHaveBeenCalledTimes(1); // first failure logged, not every one

    const good = PositionSnapshot.encode(
      PositionSnapshot.fromPartial({ meta: { msgId: "m1" }, bookId: 7 }),
    ).finish();
    conn?.emit("d1.positions.*", "d1.positions.7", good);
    const s = store.getState().subjects.positions;
    expect(s.count).toBe(1);
    expect(s.latest["7"]?.value).toMatchObject({ bookId: 7 });
  });

  test("stop(): closes the link and never dials again", async () => {
    const { script, handle } = setup(["ok"]);
    await vi.advanceTimersByTimeAsync(0);
    await handle.stop();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(script.dials()).toBe(1);
    expect(script.conns[0]?.closeCalls).toBe(1);
  });

  test("stop() during backoff cancels the pending dial", async () => {
    const { script, handle } = setup(["ok"]);
    await vi.advanceTimersByTimeAsync(0);
    script.conns[0]?.drop();
    await vi.advanceTimersByTimeAsync(100);
    await handle.stop();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(script.dials()).toBe(1);
  });
});
