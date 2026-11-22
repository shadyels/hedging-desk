/** The one state chip (DESIGN.md "State matrix"): glyph + label + outline shape, one prop. */
export type ChipState =
  "ok" | "warn" | "hard" | "halted" | "stale" | "waiting" | "disconnected";

export const CHIP_LABEL: Record<ChipState, string> = {
  ok: "OK",
  warn: "WARN",
  hard: "HARD",
  halted: "HALTED",
  stale: "STALE",
  waiting: "WAITING",
  disconnected: "DISCONNECTED",
};

interface Props {
  state: ChipState;
  /** Replaces the label word, e.g. FAULT for a health fault (HARD cue, different word). */
  label?: string;
  /** Age or waited-for clock appended to the label, e.g. "47s". */
  clock?: string | null;
}

export function StateChip({ state, label, clock }: Props) {
  return (
    <span className="chip" data-state={state}>
      <span className="chip-glyph" aria-hidden="true" />
      {label ?? CHIP_LABEL[state]}
      {clock ? ` ${clock}` : ""}
    </span>
  );
}
