import { freshness, isEventSubject } from "../bus/freshness";
import { SUBJECT_IDS, SUBJECTS, type SubjectId } from "../bus/subjects";
import { StateChip, type ChipState } from "../components/StateChip";
import { WireText } from "../components/WireText";
import { XScroll } from "../components/XScroll";
import { MISSING, formatAge } from "../format";
import type { HealthStatus } from "../gen/live";
import { NoneSince } from "../risk/parts";
import { useBus, type Entry } from "../store";

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
  // An event subject that has delivered nothing reads "none since connect": no WAITING chip, no clock.
  const none = fresh.state === "waiting" && isEventSubject(spec);
  return (
    <tr>
      <th scope="row">
        <code>{spec.pattern}</code>
      </th>
      <td>
        {none ? (
          <NoneSince />
        ) : (
          <StateChip
            state={state}
            {...(fault ? { label: "FAULT" } : {})}
            clock={clock}
          />
        )}
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

/** Every registered subject with its state: the bus diagnostic, on the Health screen. */
export function SubjectsTable({ now }: { now: number }) {
  const duplicates = useBus((s) => s.duplicates);
  return (
    <section className="panel" aria-labelledby="p-subjects">
      <div className="panel-head">
        <h3 id="p-subjects">Bus subjects</h3>
      </div>
      <XScroll label="Bus subjects table">
        <table className="subjects" aria-labelledby="p-subjects">
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
      </XScroll>
      <p className="foot">
        Duplicates dropped: {duplicates}. Counts are since connect.
      </p>
    </section>
  );
}
