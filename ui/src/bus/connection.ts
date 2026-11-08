/**
 * NATS connection manager. Owns the reconnect loop so it is explicit and testable: the nats-core
 * client is told NOT to reconnect (`reconnect: false`); when the link drops its `closed()` promise
 * resolves, and this module backs off exponentially, dials a fresh connection and re-subscribes
 * every registry subject (ui/CLAUDE.md rule 4).
 *
 * The connection layer is injected (`Connector`), so tests drive it with a fake and use the real
 * store. A decode failure is counted and logged here and never reaches the render path.
 */
import { wsconnect } from "@nats-io/nats-core";
import {
  SUBJECT_IDS,
  SUBJECTS,
  keyOf,
  type SubjectId,
  type WithMeta,
} from "./subjects";

export type ConnStatus =
  "connecting" | "connected" | "disconnected" | "reconnecting";

export interface StatusInfo {
  /** 0 while connected or on the first dial; n = nth consecutive failed attempt. */
  attempt: number;
  /** Wall-clock ms when the next dial starts; null unless status is "reconnecting". */
  nextRetryAtMs: number | null;
}

/** What the manager pushes into the store. Implemented by the store's actions. */
export interface BusSink {
  onStatus(status: ConnStatus, info: StatusInfo, atMs: number): void;
  onMessage(
    id: SubjectId,
    subject: string,
    key: string,
    msg: WithMeta,
    receivedAtMs: number,
  ): void;
  onDecodeError(id: SubjectId): void;
}

export interface BusConnection {
  subscribe(
    pattern: string,
    onMessage: (subject: string, data: Uint8Array) => void,
  ): void;
  /** Resolves when the link is gone (lost or closed). */
  closed(): Promise<unknown>;
  close(): Promise<void>;
}
export type Connector = () => Promise<BusConnection>;

/** Exponential backoff, no jitter (one UI per desk, no herd): 0.5, 1, 2, 4, 8, 15, 15... seconds. */
export const BACKOFF_BASE_MS = 500;
export const BACKOFF_CAP_MS = 15_000;
export const backoffMs = (attempt: number): number =>
  Math.min(BACKOFF_CAP_MS, BACKOFF_BASE_MS * 2 ** Math.max(0, attempt - 1));

/** The real transport: nats-core over a browser WebSocket. */
export function natsConnector(url: string): Connector {
  return async () => {
    const nc = await wsconnect({
      servers: url,
      reconnect: false, // the loop below reconnects, so backoff and resubscribe are ours
      pingInterval: 5_000, // dead-link detection ~15 s (maxPingOut 2) instead of the 2 min default
      maxPingOut: 2,
      name: "hedging-ui",
    });
    return {
      subscribe: (pattern, onMessage) => {
        nc.subscribe(pattern, {
          callback: (err, msg) => {
            if (err)
              console.warn(`[bus] subscription ${pattern}:`, err.message);
            else onMessage(msg.subject, msg.data);
          },
        });
      },
      closed: () => nc.closed(),
      close: () => nc.close(),
    };
  };
}

export interface ConnectionOpts {
  connect: Connector;
  sink: BusSink;
  log?: (...args: unknown[]) => void;
}

export function startConnection({
  connect,
  sink,
  log = console.warn,
}: ConnectionOpts): { stop(): Promise<void> } {
  let stopped = false;
  let current: BusConnection | null = null;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let wake: (() => void) | undefined;
  const errorCounts = new Map<SubjectId, number>();

  const sleep = (ms: number) =>
    new Promise<void>((resolve) => {
      wake = resolve;
      timer = setTimeout(resolve, ms);
    });

  function handle(id: SubjectId, subject: string, data: Uint8Array): void {
    const spec = SUBJECTS[id];
    try {
      const msg = spec.decoder(data);
      sink.onMessage(
        id,
        subject,
        keyOf(spec.pattern, subject),
        msg,
        Date.now(),
      );
    } catch (err) {
      const n = (errorCounts.get(id) ?? 0) + 1;
      errorCounts.set(id, n);
      sink.onDecodeError(id);
      // First failure and every 100th: a bad publisher must not flood the console.
      if (n === 1 || n % 100 === 0)
        log(`[bus] ${id} (${subject}): undecodable payload #${n}`, err);
    }
  }

  async function run(): Promise<void> {
    let attempt = 0;
    sink.onStatus(
      "connecting",
      { attempt: 0, nextRetryAtMs: null },
      Date.now(),
    );
    while (!stopped) {
      try {
        const conn = await connect();
        current = conn;
        if (stopped) {
          await conn.close();
          return;
        }
        for (const id of SUBJECT_IDS) {
          conn.subscribe(SUBJECTS[id].pattern, (subject, data) =>
            handle(id, subject, data),
          );
        }
        attempt = 0;
        sink.onStatus(
          "connected",
          { attempt: 0, nextRetryAtMs: null },
          Date.now(),
        );
        await conn.closed(); // link lost
      } catch (err) {
        log("[bus] connection failed", err);
        await current?.close().catch(() => undefined);
      }
      current = null;
      if (stopped) return;
      attempt += 1;
      const delay = backoffMs(attempt);
      const now = Date.now();
      sink.onStatus("disconnected", { attempt, nextRetryAtMs: null }, now);
      sink.onStatus(
        "reconnecting",
        { attempt, nextRetryAtMs: now + delay },
        now,
      );
      await sleep(delay);
    }
  }

  void run();

  return {
    async stop() {
      stopped = true;
      clearTimeout(timer);
      wake?.();
      await current?.close().catch(() => undefined);
    },
  };
}
