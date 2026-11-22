import type { ReactNode } from "react";
import type { Freshness } from "../bus/freshness";
import { StateChip } from "../components/StateChip";
import { formatAge, formatUtcTime } from "../format";
import { useBus } from "../store";
import { limitChipState, type Headroom, type LimitChipState } from "./model";
import type { RiskSnapshot_LimitLevel } from "../gen/live";

/**
 * "none since connect 12:04:09Z": plain text for an event subject that has delivered nothing (DESIGN.md
 * "Waiting is not stale"). An event stream has no snapshot, so silence is not a wait: no chip, no clock.
 */
export function NoneSince() {
  const since = useBus((s) => s.conn.firstConnectedAtMs);
  return (
    <span className="none-since">
      {since === null
        ? "not connected yet"
        : `none since connect ${formatUtcTime(since)}`}
    </span>
  );
}

/**
 * Panel title row. A fresh panel shows the age of its last message; anything else shows its state chip.
 * `event` marks an event-driven subject (never stale): while nothing has arrived it reads "none since
 * connect HH:MM:SSZ" as plain text instead of a WAITING chip with a running clock.
 */
export function PanelHead({
  id,
  title,
  hint,
  fresh,
  note,
  ageLabel = "last msg",
  event = false,
}: {
  id: string;
  title: ReactNode;
  /** Tooltip on the title: the NATS subject pattern and any ADR citation (kept off the visible panel). */
  hint?: string;
  fresh: Freshness;
  note?: ReactNode;
  ageLabel?: string;
  event?: boolean;
}) {
  const age = fresh.ageMs === null ? null : formatAge(fresh.ageMs);
  return (
    <div className="panel-head">
      <h3 id={id} title={hint}>
        {title}
      </h3>
      {note ? <span className="panel-note">{note}</span> : null}
      <span className="panel-status">
        {fresh.state === "ok" ? (
          age === null ? null : (
            `${ageLabel} ${age} ago`
          )
        ) : event && fresh.state === "waiting" ? (
          <NoneSince />
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

/** Body of an event panel that has received nothing: an explanation, plain text, no dotted waiting outline. The "none since connect" fact is in the panel head. */
export function NoneYetBody({ children }: { children: ReactNode }) {
  return <p className="none-yet">{children}</p>;
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
