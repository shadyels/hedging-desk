import { useMemo } from "react";
import { freshness } from "../bus/freshness";
import { SUBJECTS } from "../bus/subjects";
import { StateChip, type ChipState } from "../components/StateChip";
import { WireText } from "../components/WireText";
import { formatUtcTime } from "../format";
import type { RiskLimitAlert } from "../gen/live";
import { useBus } from "../store";
import { asAlert } from "./model";
import { PanelHead, WaitingBody } from "./parts";

/** cleared = back inside warn (OK); breached = hard; otherwise warn. */
export const alertChipState = (a: RiskLimitAlert): ChipState =>
  a.cleared ? "ok" : a.breached ? "hard" : "warn";

export function AlertTicker({ now }: { now: number }) {
  const conn = useBus((s) => s.conn);
  const st = useBus((s) => s.subjects.limitAlerts);
  const ring = st.rings[""];
  // Newest first, capped at the registry ring size (the store already bounds the ring to it).
  const rows = useMemo(
    () =>
      (ring ?? []).slice().reverse().slice(0, SUBJECTS.limitAlerts.ringSize),
    [ring],
  );
  // Event-driven subject: freshness never returns "stale", only waiting / ok (age) / disconnected.
  const fresh = freshness(SUBJECTS.limitAlerts, st.last, conn, now);
  const since =
    conn.firstConnectedAtMs === null
      ? null
      : formatUtcTime(conn.firstConnectedAtMs);
  return (
    <section className="panel" aria-labelledby="p-alerts">
      <PanelHead
        id="p-alerts"
        title="Limit alerts"
        note={<code>d1.alerts.limits</code>}
        fresh={fresh}
        ageLabel="last event"
      />
      {rows.length === 0 ? (
        <WaitingBody
          what={<code>d1.alerts.limits</code>}
          extra={
            since === null
              ? null
              : `No limit alerts since connect ${since}. Alerts are sent on level change only.`
          }
        />
      ) : (
        <ul className="ticker">
          {rows.map((e) => {
            const a = asAlert(e.value);
            if (!a) return null;
            return (
              <li key={`${e.receivedAtMs}:${e.value.meta?.msgId ?? ""}`}>
                <span className="num">{formatUtcTime(e.receivedAtMs)}</span>
                <StateChip state={alertChipState(a)} />
                <WireText value={a.text} />
              </li>
            );
          })}
        </ul>
      )}
      <p className="foot">
        Newest first, level changes only (no hysteresis). Time is when the UI
        received the alert. Text is shown as published.
      </p>
    </section>
  );
}
