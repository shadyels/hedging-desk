/**
 * Monitoring shell (P3.M1 Slice 1): connection status and per-subject state. The four screens
 * (ui/CLAUDE.md "Screens") are built on top of this in later slices.
 */
import { freshness } from "./bus/freshness";
import { SUBJECT_IDS, SUBJECTS, type SubjectId } from "./bus/subjects";
import { StateChip, type ChipState } from "./components/StateChip";
import { WireText } from "./components/WireText";
import { MISSING, formatAge, formatUtcTime } from "./format";
import type { HealthStatus } from "./gen/live";
import { useBus, type Entry } from "./store";
import { RiskBoard } from "./risk/RiskBoard";
import { useNow } from "./useNow";

const isHealth = (v: unknown): v is HealthStatus =>
  typeof v === "object" &&
  v !== null &&
  typeof (v as Partial<HealthStatus>).component === "string";

function SubjectRow({ id, now }: { id: SubjectId; now: number }) {
  const conn = useBus((s) => s.conn);
  const st = useBus((s) => s.subjects[id]);
  const spec = SUBJECTS[id];
  const fresh = freshness(spec, st.last, conn, now);
  const health = st.last && isHealth(st.last.value) ? st.last.value : null;
  const fault = fresh.state === "ok" && health !== null && !health.ok;
  const state: ChipState = fault ? "hard" : fresh.state;
  // Waiting and stale carry a clock; an event-driven subject shows its age in the Last column instead.
  const clock =
    fresh.state === "waiting" || fresh.state === "stale"
      ? fresh.ageMs === null
        ? null
        : formatAge(fresh.ageMs)
      : null;
  const last: Entry | null = st.last;
  return (
    <tr>
      <th scope="row">
        <code>{spec.pattern}</code>
      </th>
      <td>
        <StateChip
          state={state}
          {...(fault ? { label: "FAULT" } : {})}
          clock={clock}
        />
      </td>
      <td className="num r">{st.count}</td>
      <td className="num r">
        {last ? formatAge(now - last.receivedAtMs) : MISSING}
      </td>
      <td className="num r">{st.decodeErrors}</td>
      <td>
        {health ? (
          <WireText
            value={`${health.component}: ${health.ok ? "ok" : "fault"}`}
          />
        ) : null}
      </td>
    </tr>
  );
}

export function App() {
  const conn = useBus((s) => s.conn);
  const duplicates = useBus((s) => s.duplicates);
  const now = useNow();
  const up = conn.status === "connected";
  const since =
    conn.disconnectedSinceMs === null
      ? MISSING
      : formatUtcTime(conn.disconnectedSinceMs);
  const lastData =
    conn.lastDataAtMs === null
      ? "no data received"
      : `last data ${formatAge(now - conn.lastDataAtMs)} old`;
  const retry =
    conn.status === "reconnecting" && conn.nextRetryAtMs !== null
      ? `, attempt ${conn.attempt}, retry in ${formatAge(Math.max(0, conn.nextRetryAtMs - now))}`
      : "";
  return (
    <>
      <header className="bar">
        <strong>Desk Hedging</strong>
        <span className="bar-conn">
          <StateChip
            state={up ? "ok" : "disconnected"}
            label={up ? "CONNECTED" : conn.status.toUpperCase()}
          />
        </span>
      </header>
      {!up && (
        <div className="banner" role="status">
          <span className="banner-glyph" aria-hidden="true" />
          DISCONNECTED since {since} ({lastData}
          {retry})
        </div>
      )}
      <main className="board" data-disconnected={!up}>
        <RiskBoard />
        <table className="subjects">
          <caption>Bus subjects</caption>
          <thead>
            <tr>
              <th scope="col">Subject</th>
              <th scope="col">State</th>
              <th scope="col" className="r">
                Messages
              </th>
              <th scope="col" className="r">
                Last
              </th>
              <th scope="col" className="r">
                Decode errors
              </th>
              <th scope="col">Detail</th>
            </tr>
          </thead>
          <tbody>
            {SUBJECT_IDS.map((id) => (
              <SubjectRow key={id} id={id} now={now} />
            ))}
          </tbody>
        </table>
        <p className="foot">
          Duplicates dropped: {duplicates}. Counts are since connect.
        </p>
      </main>
    </>
  );
}
