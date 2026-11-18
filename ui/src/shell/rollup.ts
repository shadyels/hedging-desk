/**
 * "Can I trust the numbers right now": the worst state across the four screens, by the DESIGN.md precedence
 * (disconnected > waiting > halted > stale > HARD > WARN > OK). `rollup` is pure; `useRollup` gathers the findings.
 */
import { freshness } from "../bus/freshness";
import { SUBJECTS, type SubjectId } from "../bus/subjects";
import { CHIP_LABEL, type ChipState } from "../components/StateChip";
import { formatAge } from "../format";
import { asHealth } from "../health/model";
import { asRisk, limitChipState } from "../risk/model";
import { useBookViews } from "../risk/useBookViews";
import { instrumentLabel } from "../refdata";
import { useBus } from "../store";

/** Worst first. */
export const PRECEDENCE: readonly ChipState[] = [
  "disconnected",
  "waiting",
  "halted",
  "stale",
  "hard",
  "warn",
  "ok",
];

export interface Finding {
  state: ChipState;
  why: string;
  /** Replaces the chip word, e.g. FAULT for a health fault (HARD cue). */
  label?: string;
}
export interface Rollup {
  state: ChipState;
  label: string;
  /** Every finding that is not OK, worst first. */
  reasons: string[];
}

const rank = (s: ChipState) => PRECEDENCE.indexOf(s);

export function rollup(findings: readonly Finding[]): Rollup {
  const sorted = [...findings].sort((a, b) => rank(a.state) - rank(b.state));
  const worst = sorted[0];
  if (!worst || worst.state === "ok")
    return { state: "ok", label: CHIP_LABEL.ok, reasons: [] };
  return {
    state: worst.state,
    label: worst.label ?? CHIP_LABEL[worst.state],
    reasons: sorted.filter((f) => f.state !== "ok").map((f) => f.why),
  };
}

/** Cadence subjects: silence is a state. The tracker is not here: the bus does not say which books track, so never-seen is not a finding. */
const CADENCE: SubjectId[] = [
  "riskFirm",
  "d1Health",
  "exoHealth",
  "valuations",
  "positions",
];

export function useRollup(now: number): Rollup {
  const conn = useBus((s) => s.conn);
  const subjects = useBus((s) => s.subjects);
  const views = useBookViews();
  if (conn.status !== "connected")
    return rollup([{ state: "disconnected", why: "NATS link is down" }]);
  const out: Finding[] = [];
  for (const id of [...CADENCE, "tracker" as const]) {
    const spec = SUBJECTS[id];
    const f = freshness(spec, subjects[id].last, conn, now);
    if (f.state === "waiting" && id !== "tracker")
      out.push({
        state: "waiting",
        why: `${spec.pattern}: nothing received yet`,
      });
    if (f.state === "stale")
      out.push({
        state: "stale",
        why: `${spec.pattern}: stale ${f.ageMs === null ? "" : formatAge(f.ageMs)}`.trim(),
      });
  }
  for (const id of ["d1Health", "exoHealth"] as const) {
    if (asHealth(subjects[id].last?.value)?.ok === false)
      out.push({
        state: "hard",
        label: "FAULT",
        why: `${SUBJECTS[id].pattern}: ok = false`,
      });
  }
  for (const g of asRisk(subjects.riskFirm.last?.value)?.greeks ?? []) {
    const sym = instrumentLabel(g.underlying?.instrumentId ?? 0);
    for (const [greek, level] of [
      ["vega", g.vegaLevel],
      ["gamma", g.gammaLevel],
      ["rho", g.rhoLevel],
    ] as const) {
      const state = limitChipState(level);
      if (state === "warn" || state === "hard")
        out.push({ state, why: `${sym} ${greek} ${CHIP_LABEL[state]}` });
      else if (state === "waiting")
        out.push({ state, why: `${sym} ${greek}: limit level not populated` });
    }
  }
  for (const v of views)
    if (v.haltReasons.length > 0)
      out.push({
        state: "halted",
        why: `book ${v.bookId} halted: ${v.haltReasons.join(", ")}`,
      });
  return rollup(out);
}
