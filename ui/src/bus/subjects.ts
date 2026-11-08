/**
 * Registry of every NATS subject the monitoring UI subscribes to (ui/CLAUDE.md rule 3).
 * A subject not in protocol/nats-subjects.md does not exist: bus/subjects.test.ts parses that
 * file and fails if this registry and the table diverge (pattern, payload type, UI as consumer).
 *
 * Staleness thresholds live HERE and nowhere else (DESIGN.md "Thresholds"): components read the
 * registry, they never carry a literal.
 */
import type { Meta } from "../gen/common";
import {
  ExecutionReport,
  HealthStatus,
  InternalCrossNotice,
  MarkPrice,
  PositionSnapshot,
  ReconAlert,
  RiskLimitAlert,
  RiskSnapshot,
  TargetPosition,
  TrackerAnalytics,
  ValuationSnapshot,
} from "../gen/live";

/** Every payload carries a Meta block (protocol/CLAUDE.md rule 5); the store dedupes on meta.msgId. */
export interface WithMeta {
  meta: Meta | undefined;
}

/** Event-driven subject: shows the age of the last event and is never stale. */
export const NEVER = "never";
/** DESIGN.md leaves the threshold open. Shown like an event (age, no verdict) until it is decided. */
export const UNDECIDED = "undecided";

export type StaleAfter =
  number | ((msg: WithMeta) => number) | typeof NEVER | typeof UNDECIDED;

export interface SubjectSpec {
  /** Subscription pattern. `*` stands for one `<book>` / `<instrument>` token, as in nats-subjects.md. */
  pattern: string;
  /** Protobuf message name, checked against the payload column of nats-subjects.md. */
  payload: string;
  decoder: (bytes: Uint8Array) => WithMeta;
  staleAfterMs: StaleAfter;
  /** Ring buffer capacity (0 = latest-wins only). */
  ringSize: number;
  /** Ring per subject key (sparkline per book) or one ring for the whole subject (ticker, blotter). */
  ringBy: "key" | "subject";
}

/** One cast, sound by construction: the decoder produced T, so the wrapped function receives T. */
function subject<T extends WithMeta>(s: {
  pattern: string;
  payload: string;
  decoder: (bytes: Uint8Array) => T;
  staleAfterMs: number | ((msg: T) => number) | typeof NEVER | typeof UNDECIDED;
  ringSize: number;
  ringBy?: "key" | "subject";
}): SubjectSpec {
  const stale = s.staleAfterMs;
  return {
    ...s,
    ringBy: s.ringBy ?? "subject",
    staleAfterMs: typeof stale === "function" ? (m) => stale(m as T) : stale,
  };
}

// Named thresholds. Cadence is the producer's publish interval (protocol/nats-subjects.md).
const THREE_S = 3_000; // 1 s cadence (health, positions, risk.firm): three missed beats
const VALUATION_STALE_MS = 12_000; // 5 s republish (exo.toml revaluation_interval_s)
const TRACKER_STALE_FACTOR = 2.5; // x samplingIntervalS from the message
// ponytail: valuations at 12 s sits below D1's own valuation_max_age_s = 15 (delta-one/d1.toml), so the UI
// can read STALE up to 3 s before D1 forces WARN; DESIGN.md's proposal was 15 s. Decide in review.

// ponytail: ring sizes are untuned guesses sized for one trading session. Revisit with real volumes.
export const SUBJECTS = {
  riskFirm: subject({
    pattern: "d1.risk.firm",
    payload: "RiskSnapshot",
    decoder: (b) => RiskSnapshot.decode(b),
    staleAfterMs: THREE_S,
    ringSize: 0,
  }),
  limitAlerts: subject({
    pattern: "d1.alerts.limits",
    payload: "RiskLimitAlert",
    decoder: (b) => RiskLimitAlert.decode(b),
    staleAfterMs: NEVER,
    ringSize: 200,
  }),
  positions: subject({
    pattern: "d1.positions.*",
    payload: "PositionSnapshot",
    decoder: (b) => PositionSnapshot.decode(b),
    staleAfterMs: THREE_S,
    ringSize: 0,
  }),
  targets: subject({
    pattern: "exo.targets.*.*",
    payload: "TargetPosition",
    decoder: (b) => TargetPosition.decode(b),
    staleAfterMs: NEVER,
    ringSize: 0,
  }),
  valuations: subject({
    pattern: "exo.valuations.*",
    payload: "ValuationSnapshot",
    decoder: (b) => ValuationSnapshot.decode(b),
    staleAfterMs: VALUATION_STALE_MS,
    ringSize: 0,
  }),
  reconAlerts: subject({
    pattern: "exo.alerts.recon",
    payload: "ReconAlert",
    decoder: (b) => ReconAlert.decode(b),
    staleAfterMs: NEVER,
    ringSize: 200,
  }),
  exec: subject({
    pattern: "d1.exec.*.*",
    payload: "ExecutionReport",
    decoder: (b) => ExecutionReport.decode(b),
    staleAfterMs: NEVER,
    ringSize: 5000,
  }),
  crosses: subject({
    pattern: "d1.crosses",
    payload: "InternalCrossNotice",
    decoder: (b) => InternalCrossNotice.decode(b),
    staleAfterMs: NEVER,
    ringSize: 2000,
  }),
  tracker: subject({
    pattern: "d1.tracker.*",
    payload: "TrackerAnalytics",
    decoder: (b) => TrackerAnalytics.decode(b),
    // A message with samplingIntervalS = 0 yields 0 ms: stale at once, which is the safe reading of a bad contract.
    staleAfterMs: (m: TrackerAnalytics) =>
      m.samplingIntervalS * TRACKER_STALE_FACTOR * 1000,
    ringSize: 120,
    ringBy: "key",
  }),
  d1Health: subject({
    pattern: "d1.health",
    payload: "HealthStatus",
    decoder: (b) => HealthStatus.decode(b),
    staleAfterMs: THREE_S,
    ringSize: 0,
  }),
  exoHealth: subject({
    pattern: "exo.health",
    payload: "HealthStatus",
    decoder: (b) => HealthStatus.decode(b),
    staleAfterMs: THREE_S,
    ringSize: 0,
  }),
  marks: subject({
    pattern: "d1.marks.*",
    payload: "MarkPrice",
    decoder: (b) => MarkPrice.decode(b),
    staleAfterMs: UNDECIDED,
    ringSize: 0,
  }),
} satisfies Record<string, SubjectSpec>;

export type SubjectId = keyof typeof SUBJECTS;
export const SUBJECT_IDS = Object.keys(SUBJECTS) as SubjectId[];

/** The wildcard tokens of a concrete subject, joined: ("exo.targets.*.*", "exo.targets.3.7") -> "3.7"; fixed subjects -> "". */
export function keyOf(pattern: string, subj: string): string {
  const p = pattern.split(".");
  const s = subj.split(".");
  return p.flatMap((tok, i) => (tok === "*" ? [s[i] ?? ""] : [])).join(".");
}

/** Resolve the staleness threshold for one message; null = no time-based staleness. */
export function staleThresholdMs(
  spec: SubjectSpec,
  msg: WithMeta,
): number | null {
  const s = spec.staleAfterMs;
  if (typeof s === "number") return s;
  if (typeof s === "function") return s(msg);
  return null;
}
