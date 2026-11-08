/**
 * Thin subscription store (Zustand v5) fed by the bus connection (ui/CLAUDE.md "Stack").
 *
 * - keyed subjects: latest-wins map `latest[key]`; series subjects: bounded ring buffers.
 * - writes are batched: `ingest` queues, one `set()` per animation frame (250 ms timer when
 *   requestAnimationFrame is absent) so a 4 Hz x N-instrument mark feed is one render, not N.
 * - dedupe on Meta.msg_id (root invariant 4) through a bounded LRU.
 *
 * Zustand v5 rule: a selector must return a primitive or a reference already in state. A selector
 * that builds a fresh object/array re-renders forever; use `useShallow` if one is unavoidable.
 */
import { useStore } from "zustand";
import { createStore } from "zustand/vanilla";
import type { BusSink, ConnStatus, StatusInfo } from "./bus/connection";
import {
  SUBJECT_IDS,
  SUBJECTS,
  type SubjectId,
  type WithMeta,
} from "./bus/subjects";

export interface Entry {
  value: WithMeta;
  receivedAtMs: number;
}

export interface SubjectState {
  /** Messages accepted (after dedupe) this session. 0 = never received = WAITING, not stale. */
  count: number;
  /** Most recent message across all keys. */
  last: Entry | null;
  decodeErrors: number;
  /** Latest message per key ("" for subjects without wildcards). */
  latest: Record<string, Entry>;
  /** Ring buffers, oldest first. Keyed by subject key or "" depending on the registry's ringBy. */
  rings: Record<string, Entry[]>;
}

export interface ConnState {
  status: ConnStatus;
  attempt: number;
  nextRetryAtMs: number | null;
  /** First time the link came up this session; the WAITING clock starts here. */
  firstConnectedAtMs: number | null;
  /** Latest (re)connect; data older than this reads stale until a fresh message arrives. */
  connectedAtMs: number | null;
  /** When the current outage began; null while connected. */
  disconnectedSinceMs: number | null;
  lastDataAtMs: number | null;
}

export interface BusState {
  conn: ConnState;
  subjects: Record<SubjectId, SubjectState>;
  /** Messages dropped as msg_id duplicates. */
  duplicates: number;
}

const emptySubject = (): SubjectState => ({
  count: 0,
  last: null,
  decodeErrors: 0,
  latest: {},
  rings: {},
});

export const initialState = (): BusState => ({
  conn: {
    status: "connecting",
    attempt: 0,
    nextRetryAtMs: null,
    firstConnectedAtMs: null,
    connectedAtMs: null,
    disconnectedSinceMs: Date.now(), // not connected since boot
    lastDataAtMs: null,
  },
  subjects: Object.fromEntries(
    SUBJECT_IDS.map((id) => [id, emptySubject()]),
  ) as Record<SubjectId, SubjectState>,
  duplicates: 0,
});

/** Bounded set with LRU eviction. `seen` returns true for a repeat, and refreshes it. */
export class MsgIdLru {
  private readonly ids = new Set<string>();
  constructor(private readonly cap: number) {}
  seen(id: string): boolean {
    const hit = this.ids.delete(id);
    this.ids.add(id);
    if (this.ids.size > this.cap) {
      const oldest = this.ids.values().next();
      if (!oldest.done) this.ids.delete(oldest.value);
    }
    return hit;
  }
  get size(): number {
    return this.ids.size;
  }
}

export interface Pending {
  id: SubjectId;
  key: string;
  entry: Entry;
}

/** Fold a batch into state. Copies only what the batch touches; untouched subjects keep their reference. */
export function applyBatch(prev: BusState, batch: Pending[]): BusState {
  const subjects = { ...prev.subjects };
  const ownedSubjects = new Set<SubjectId>();
  const ownedRings = new Set<string>();
  let lastDataAtMs = prev.conn.lastDataAtMs;
  for (const { id, key, entry } of batch) {
    if (!ownedSubjects.has(id)) {
      const s = subjects[id];
      subjects[id] = { ...s, latest: { ...s.latest }, rings: { ...s.rings } };
      ownedSubjects.add(id);
    }
    const st = subjects[id];
    st.count += 1;
    st.last = entry;
    st.latest[key] = entry;
    const { ringSize, ringBy } = SUBJECTS[id];
    if (ringSize > 0) {
      const rk = ringBy === "key" ? key : "";
      const owned = `${id}|${rk}`;
      const ring = ownedRings.has(owned)
        ? (st.rings[rk] ?? [])
        : (st.rings[rk] ?? []).slice();
      ownedRings.add(owned);
      ring.push(entry);
      if (ring.length > ringSize) ring.splice(0, ring.length - ringSize);
      st.rings[rk] = ring;
    }
    lastDataAtMs = entry.receivedAtMs;
  }
  return { ...prev, subjects, conn: { ...prev.conn, lastDataAtMs } };
}

export type Schedule = (flush: () => void) => void;

/** rAF, raced against a 1 s timer so a hidden tab (rAF paused) still drains; plain 250 ms timer without rAF. */
export const defaultSchedule: Schedule = (flush) => {
  if (typeof requestAnimationFrame !== "function") {
    setTimeout(flush, 250);
    return;
  }
  let done = false;
  const once = () => {
    if (done) return;
    done = true;
    clearTimeout(t);
    flush();
  };
  const t = setTimeout(once, 1_000);
  requestAnimationFrame(once);
};

export interface BusStoreOpts {
  schedule?: Schedule;
  lruSize?: number;
}

export function createBusStore({
  schedule = defaultSchedule,
  lruSize = 20_000,
}: BusStoreOpts = {}) {
  const store = createStore<BusState>()(() => initialState());
  const lru = new MsgIdLru(lruSize);
  let pending: Pending[] = [];
  let scheduled = false;

  const flush = () => {
    scheduled = false;
    const batch = pending;
    pending = [];
    if (batch.length > 0) store.setState((s) => applyBatch(s, batch));
  };

  const sink: BusSink = {
    onStatus(status: ConnStatus, info: StatusInfo, atMs: number) {
      store.setState((s) => {
        const up = status === "connected";
        const wasUp = s.conn.status === "connected";
        return {
          conn: {
            ...s.conn,
            status,
            attempt: info.attempt,
            nextRetryAtMs: info.nextRetryAtMs,
            firstConnectedAtMs: up
              ? (s.conn.firstConnectedAtMs ?? atMs)
              : s.conn.firstConnectedAtMs,
            connectedAtMs: up ? atMs : s.conn.connectedAtMs,
            // Outage start: first non-connected status after being up (or at boot); kept across disconnected/reconnecting.
            disconnectedSinceMs: up
              ? null
              : wasUp || s.conn.disconnectedSinceMs === null
                ? atMs
                : s.conn.disconnectedSinceMs,
          },
        };
      });
    },
    onMessage(id, _subject, key, msg, receivedAtMs) {
      const msgId = msg.meta?.msgId ?? "";
      if (msgId !== "" && lru.seen(msgId)) {
        store.setState((s) => ({ duplicates: s.duplicates + 1 }));
        return;
      }
      pending.push({ id, key, entry: { value: msg, receivedAtMs } });
      if (!scheduled) {
        scheduled = true;
        schedule(flush);
      }
    },
    onDecodeError(id) {
      store.setState((s) => ({
        subjects: {
          ...s.subjects,
          [id]: {
            ...s.subjects[id],
            decodeErrors: s.subjects[id].decodeErrors + 1,
          },
        },
      }));
    },
  };

  /** Drop everything (tests, and "clear session"). Pending messages are discarded too. */
  const reset = () => {
    pending = [];
    store.setState(initialState(), true);
  };

  return { store, sink, flush, reset };
}

// The app singleton. main.tsx wires its sink to the connection; components read it through useBus.
const bus = createBusStore();
export const busStore = bus.store;
export const busSink = bus.sink;
export const flushBus = bus.flush;
export const resetBus = bus.reset;
export const useBus = <U>(selector: (s: BusState) => U): U =>
  useStore(busStore, selector);
