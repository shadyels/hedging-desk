import type { ReactNode } from "react";
import type { Freshness } from "../bus/freshness";
import { StateChip } from "../components/StateChip";
import { formatAge } from "../format";
import { limitChipState, type Headroom, type LimitChipState } from "./model";
import type { RiskSnapshot_LimitLevel } from "../gen/live";

/** Panel title row. A fresh panel shows the age of its last message; anything else shows its state chip. */
export function PanelHead({
  id,
  title,
  fresh,
  note,
  ageLabel = "last msg",
}: {
  id: string;
  title: ReactNode;
  fresh: Freshness;
  note?: ReactNode;
  ageLabel?: string;
}) {
  const age = fresh.ageMs === null ? null : formatAge(fresh.ageMs);
  return (
    <div className="panel-head">
      <h2 id={id}>{title}</h2>
      {note ? <span className="panel-note">{note}</span> : null}
      <span className="panel-status">
        {fresh.state === "ok" ? (
          age === null ? null : (
            `${ageLabel} ${age} ago`
          )
        ) : (
          <StateChip
            state={fresh.state}
            clock={fresh.state === "disconnected" ? null : age}
          />
        )}
      </span>
    </div>
  );
}

/** "Never received" body: a dotted placeholder, no number at all (DESIGN.md "waiting"). */
export function WaitingBody({
  what,
  extra,
}: {
  what: ReactNode;
  extra?: ReactNode;
}) {
  return (
    <p className="waiting">
      <span aria-hidden="true">┈┈┈┈┈┈</span> Waiting for the first {what}{" "}
      message. Nothing is shown rather than a zero.
      {extra ? <> {extra}</> : null}
    </p>
  );
}

const TRAIL = { warn: "▲", hard: "✕" } as const;

/**
 * One limit chip. Precedence (DESIGN.md): waiting (level not populated) > stale > level. A stale cell keeps
 * its last WARN/HARD as a small trailing glyph so a breach is never hidden by staleness.
 */
export function LimitChip({
  level,
  fresh,
}: {
  level: RiskSnapshot_LimitLevel;
  fresh: Freshness;
}) {
  const st = limitChipState(level);
  if (st !== "waiting" && fresh.state === "stale") {
    const trail = st === "ok" ? null : TRAIL[st];
    return (
      <>
        <StateChip
          state="stale"
          clock={fresh.ageMs === null ? null : formatAge(fresh.ageMs)}
        />
        {trail ? (
          <span className="trail" title={`last level ${st.toUpperCase()}`}>
            <span aria-hidden="true">{trail}</span>
            <span className="sr-only">last level {st.toUpperCase()}</span>
          </span>
        ) : null}
      </>
    );
  }
  return <StateChip state={st} />;
}

/** 56 x 6 headroom bar: |value| / hard cap, warn tick always drawn, `»` beyond hard. Hue follows the wire level, not our own compare. */
export function HeadroomBar({
  h,
  state,
  live,
}: {
  h: Extract<Headroom, { kind: "bar" }>;
  state: LimitChipState;
  /** false while stale: the bar stays neutral. */
  live: boolean;
}) {
  const waiting = state === "waiting";
  const pct = Math.floor(h.fillBps / 100);
  return (
    <span
      className="hbar"
      data-level={waiting || !live ? "none" : state}
      data-waiting={waiting}
      role="img"
      aria-label={
        waiting
          ? "headroom unknown"
          : `${pct}% of hard limit${h.over ? ", beyond hard" : ""}`
      }
    >
      <span className="hbar-track">
        {waiting ? null : (
          <span
            className="hbar-fill"
            style={{ width: `${h.fillBps / 100}%` }}
          />
        )}
        {h.warnBps === null ? null : (
          <span className="hbar-tick" style={{ left: `${h.warnBps / 100}%` }} />
        )}
      </span>
      {h.over && !waiting ? <span aria-hidden="true">»</span> : null}
    </span>
  );
}
