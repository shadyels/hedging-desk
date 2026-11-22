/**
 * Pure system-health logic: wire guards, detail-map parsing, token -> cue mapping, u64 parsing and the
 * git_sha provenance check. No React, no clock.
 *
 * Contracts read for this file: ADR-014 Amendment 1 s2 (`d1.health` keys and the `ok` rule), ADR-014 s12 and
 * its 2026-10-25 clarification (`exo.health` keys), exo/src/exo/portfolio/state.py `health()` (the producer),
 * protocol/proto/common.proto `ValuationMeta`.
 *
 * Every detail key and value is untrusted display data (ADR-014 s14). Keys are matched against fixed
 * names or a digits-only pattern; anything else is kept verbatim in `other` and rendered as text only.
 * Token lookups use a Map, never a plain object, so a wire value such as "constructor" cannot hit a prototype.
 */
import type { WithMeta } from "../bus/subjects";
import type { ChipState } from "../components/StateChip";
import type { HealthStatus } from "../gen/live";

export const asHealth = (v: WithMeta | undefined): HealthStatus | null =>
  v &&
  "component" in v &&
  typeof v.component === "string" &&
  "detail" in v &&
  typeof v.detail === "object" &&
  v.detail !== null
    ? (v as HealthStatus)
    : null;

// ---- Token -> cue ---------------------------------------------------------------------------

/** "neutral" is not a fault and not live data: a plain outlined label, no hue (e.g. valuation `none`). */
export interface Cue {
  tone: ChipState | "neutral";
  label: string;
}

const OK: Cue = { tone: "ok", label: "OK" };
const FAULT: Cue = { tone: "hard", label: "FAULT" };
export const NOT_PUBLISHED: Cue = { tone: "neutral", label: "NOT PUBLISHED" };
export const UNRECOGNISED: Cue = { tone: "warn", label: "UNRECOGNISED" };

export type TokenKind =
  | "nats"
  | "fix"
  | "posttrade"
  | "valuation"
  | "publish"
  | "inFlight"
  | "exoBook"
  | "limit";

const TOKENS: Record<TokenKind, ReadonlyMap<string, Cue>> = {
  // ADR-014 A1 s2
  nats: new Map<string, Cue>([
    ["connected", OK],
    ["pending", { tone: "warn", label: "PENDING" }],
    ["disconnected", FAULT],
  ]),
  fix: new Map<string, Cue>([
    ["logged_on", OK],
    ["logged_out", FAULT],
  ]),
  posttrade: new Map<string, Cue>([
    ["disabled", { tone: "neutral", label: "DISABLED" }],
    ["starting", { tone: "warn", label: "STARTING" }],
    ["running", OK],
    ["failed", FAULT],
  ]),
  // `none` = never valued and never rejected: normal for some books. `stale` and `invalid` force WARN (ADR-014 s3).
  valuation: new Map<string, Cue>([
    ["none", { tone: "neutral", label: "NONE" }],
    ["ok", OK],
    ["stale", { tone: "warn", label: "STALE" }],
    ["invalid", { tone: "warn", label: "INVALID" }],
  ]),
  // ADR-014 s12: `enabled` or `refused: dirty tree ...` (a reason follows, so the refusal is matched by prefix below)
  publish: new Map<string, Cue>([["enabled", OK]]),
  inFlight: new Map<string, Cue>([
    ["true", { tone: "neutral", label: "IN FLIGHT" }],
    ["false", { tone: "neutral", label: "IDLE" }],
  ]),
  exoBook: new Map<string, Cue>([
    ["publishing", OK],
    ["awaiting_snapshot", { tone: "neutral", label: "AWAITING SNAPSHOT" }],
    ["awaiting_marks", { tone: "neutral", label: "AWAITING MARKS" }],
  ]),
  // exo.health limit.<instrument>.<greek> = the latest non-cleared alert level (state.py: "hard" if breached else "warn")
  limit: new Map<string, Cue>([
    ["warn", { tone: "warn", label: "WARN" }],
    ["hard", { tone: "hard", label: "HARD" }],
  ]),
};

const HALTED_RE = /^halted:[a-z-]{1,40}$/;
const REFUSED_RE = /^refused: /;

/** One wire token -> its cue. `undefined` = the key is absent; anything outside the contract is UNRECOGNISED, never OK. */
export function cueFor(kind: TokenKind, raw: string | undefined): Cue {
  if (raw === undefined) return NOT_PUBLISHED;
  const hit = TOKENS[kind].get(raw);
  if (hit) return hit;
  if (kind === "exoBook" && HALTED_RE.test(raw))
    return { tone: "halted", label: "HALTED" };
  if (kind === "publish" && REFUSED_RE.test(raw)) return FAULT;
  return UNRECOGNISED;
}

/** The boolean `ok` field of HealthStatus (not a detail key). */
export const okCue = (ok: boolean): Cue => (ok ? OK : FAULT);

// ---- u64 ------------------------------------------------------------------------------------

export const U64_MAX = (1n << 64n) - 1n;
export type U64 = { ok: true; value: bigint } | { ok: false; raw: string };

/** Decimal u64 -> bigint. Never `Number`; anything else is an error to show as text, never a zero. */
export function parseU64(raw: string): U64 {
  if (!/^[0-9]{1,20}$/.test(raw)) return { ok: false, raw };
  const value = BigInt(raw);
  return value <= U64_MAX ? { ok: true, value } : { ok: false, raw };
}

// ---- d1.health ------------------------------------------------------------------------------

/** Digits only, u32-sized: safe as a React key and in a label. */
const BOOK_ID = "([0-9]{1,10})";
const D1_BOOK_RE = new RegExp(
  `^limits\\.book\\.${BOOK_ID}\\.(rejects|valuation)$`,
);
const EXO_BOOK_RE = new RegExp(
  `^book\\.${BOOK_ID}\\.(state|last_valuation_ns|last_error)$`,
);
const EXO_LIMIT_RE = new RegExp(`^limit\\.${BOOK_ID}\\.(vega|gamma|rho)$`);

export type Pair = readonly [key: string, value: string];

export interface D1Book {
  id: string;
  rejects: string | undefined;
  valuation: string | undefined;
}

export interface D1Health {
  component: string;
  ok: boolean;
  nats: string | undefined;
  fix: string | undefined;
  posttrade: string | undefined;
  sendFailures: string | undefined;
  unattributable: string | undefined;
  books: D1Book[];
  /** Detail entries outside the ADR-014 A1 s2 key set, verbatim. Text only. */
  other: Pair[];
}

const byId = (a: { id: string }, b: { id: string }) =>
  Number(a.id) - Number(b.id); // ids are <= 10 digits: exact as a number, and an id, not money

export function parseD1(h: HealthStatus): D1Health {
  const out: D1Health = {
    component: h.component,
    ok: h.ok,
    nats: undefined,
    fix: undefined,
    posttrade: undefined,
    sendFailures: undefined,
    unattributable: undefined,
    books: [],
    other: [],
  };
  const books = new Map<string, D1Book>();
  for (const [k, v] of Object.entries(h.detail)) {
    if (k === "nats") out.nats = v;
    else if (k === "fix.session") out.fix = v;
    else if (k === "posttrade") out.posttrade = v;
    else if (k === "posttrade.send_failures") out.sendFailures = v;
    else if (k === "limits.unattributable_rejects") out.unattributable = v;
    else {
      const m = D1_BOOK_RE.exec(k);
      if (m?.[1] !== undefined && m[2] !== undefined) {
        const b = books.get(m[1]) ?? {
          id: m[1],
          rejects: undefined,
          valuation: undefined,
        };
        if (m[2] === "rejects") b.rejects = v;
        else b.valuation = v;
        books.set(m[1], b);
      } else out.other.push([k, v]);
    }
  }
  out.books = [...books.values()].sort(byId);
  return out;
}

// ---- exo.health -----------------------------------------------------------------------------

export interface ExoBook {
  id: string;
  state: string | undefined;
  lastValuationNs: string | undefined;
  lastError: string | undefined;
}
export interface ExoLimit {
  instrumentId: string;
  greek: string;
  level: string;
}
export interface ExoHealth {
  component: string;
  ok: boolean;
  gitSha: string | undefined;
  publish: string | undefined;
  inFlight: string | undefined;
  nats: string | undefined;
  rejectedTotal: string | undefined;
  fatal: string | undefined;
  books: ExoBook[];
  limits: ExoLimit[];
  other: Pair[];
}

export function parseExo(h: HealthStatus): ExoHealth {
  const out: ExoHealth = {
    component: h.component,
    ok: h.ok,
    gitSha: undefined,
    publish: undefined,
    inFlight: undefined,
    nats: undefined,
    rejectedTotal: undefined,
    fatal: undefined,
    books: [],
    limits: [],
    other: [],
  };
  const books = new Map<string, ExoBook>();
  for (const [k, v] of Object.entries(h.detail)) {
    if (k === "git_sha") out.gitSha = v;
    else if (k === "publish") out.publish = v;
    else if (k === "valuation_in_flight") out.inFlight = v;
    else if (k === "nats") out.nats = v;
    else if (k === "rejected_total") out.rejectedTotal = v;
    else if (k === "fatal") out.fatal = v;
    else {
      const b = EXO_BOOK_RE.exec(k);
      const l = EXO_LIMIT_RE.exec(k);
      if (b?.[1] !== undefined && b[2] !== undefined) {
        const row = books.get(b[1]) ?? {
          id: b[1],
          state: undefined,
          lastValuationNs: undefined,
          lastError: undefined,
        };
        if (b[2] === "state") row.state = v;
        else if (b[2] === "last_valuation_ns") row.lastValuationNs = v;
        else row.lastError = v;
        books.set(b[1], row);
      } else if (l?.[1] !== undefined && l[2] !== undefined) {
        out.limits.push({ instrumentId: l[1], greek: l[2], level: v });
      } else out.other.push([k, v]);
    }
  }
  out.books = [...books.values()].sort(byId);
  out.limits.sort(
    (a, b) =>
      Number(a.instrumentId) - Number(b.instrumentId) ||
      a.greek.localeCompare(b.greek),
  );
  return out;
}

// ---- git_sha provenance (root invariant 7) --------------------------------------------------

export interface ShaSource {
  label: string;
  sha: string;
}
export type ShaCheck =
  | { status: "mismatch"; sources: ShaSource[] }
  | { status: "consistent"; sha: string; count: number }
  | { status: "insufficient"; count: number };

/**
 * Comparable values: exo.health `git_sha` and each exo.valuations `valuation.git_sha`. Both are produced by
 * EXO's one `git_sha()` (exo/src/exo/provenance.py), so equal builds give equal strings, `-dirty` included.
 * Excluded: an empty string and exo.health's `unavailable` placeholder (not a sha). `d1.risk.firm` carries no
 * git_sha, so Delta One is not part of this check. At least two comparable values are needed to say anything.
 */
export function checkGitSha(
  exoHealthSha: string | undefined,
  books: readonly { bookId: number; sha: string }[],
): ShaCheck {
  const usable = (s: string | undefined): s is string =>
    s !== undefined && s !== "" && s !== "unavailable";
  const sources: ShaSource[] = [];
  if (usable(exoHealthSha))
    sources.push({ label: "exo.health", sha: exoHealthSha });
  for (const b of books)
    if (usable(b.sha)) sources.push({ label: `book ${b.bookId}`, sha: b.sha });
  if (sources.length < 2)
    return { status: "insufficient", count: sources.length };
  const first = sources[0]?.sha;
  if (sources.every((s) => s.sha === first))
    return { status: "consistent", sha: first ?? "", count: sources.length };
  return { status: "mismatch", sources };
}
