/**
 * Freshness of one value, per the DESIGN.md state matrix. Pure: callers pass `nowMs` from their own
 * ticking clock so ages advance locally between messages.
 *
 *   disconnected  link is down (outranks everything)
 *   waiting       connected, and nothing has EVER arrived for this key. Never escalates to stale.
 *   stale         something arrived before and it is older than the threshold, or predates the latest reconnect
 *   ok            fresh, or an event-driven / undecided subject (those show an age, never a verdict)
 */
import type { ConnState, Entry } from "../store";
import { staleThresholdMs, type SubjectSpec } from "./subjects";

export type FreshState = "disconnected" | "waiting" | "stale" | "ok";

export interface Freshness {
  state: FreshState;
  /** waiting: time spent waiting. stale/ok: age of the last message. disconnected: age of last data, if any. null: unknown. */
  ageMs: number | null;
}

export function freshness(
  spec: SubjectSpec,
  entry: Entry | null | undefined,
  conn: ConnState,
  nowMs: number,
): Freshness {
  const age = entry ? Math.max(0, nowMs - entry.receivedAtMs) : null;
  if (conn.status !== "connected") return { state: "disconnected", ageMs: age };
  if (!entry) {
    return {
      state: "waiting",
      ageMs:
        conn.firstConnectedAtMs === null
          ? null
          : Math.max(0, nowMs - conn.firstConnectedAtMs),
    };
  }
  const threshold = staleThresholdMs(spec, entry.value);
  const predatesReconnect =
    threshold !== null &&
    conn.connectedAtMs !== null &&
    entry.receivedAtMs < conn.connectedAtMs;
  const stale =
    threshold !== null && ((age ?? 0) > threshold || predatesReconnect);
  return { state: stale ? "stale" : "ok", ageMs: age };
}
