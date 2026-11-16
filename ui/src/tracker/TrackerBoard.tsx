import { useMemo } from "react";
import { freshness } from "../bus/freshness";
import { SUBJECTS } from "../bus/subjects";
import { WireText } from "../components/WireText";
import {
  MISSING,
  formatAge,
  formatBpsE9,
  formatPctE9,
  formatUtcTime,
} from "../format";
import type { TrackerAnalytics } from "../gen/live";
import { PanelHead, WaitingBody } from "../risk/parts";
import { useBus, type Entry } from "../store";
import {
  DEMO_TITLE,
  PRICE_ONLY_NOTE,
  TRACKER_BASIS_IS_DEMO,
  asTracker,
  kindLabel,
  pointsAttr,
  sparkPoints,
  teSeries,
  windowSpanMs,
} from "./model";

const W = 200;
const H = 28;

/** TE trend from the bounded ring. 1 px neutral line (DESIGN.md "Sparkline"); the x axis is arrival time. */
function Sparkline({ ring }: { ring: readonly Entry[] | undefined }) {
  const s = teSeries(ring);
  const pts = sparkPoints(s, W, H);
  const first = s[0];
  const last = s[s.length - 1];
  const label =
    first && last
      ? `TE, ${s.length} sample${s.length === 1 ? "" : "s"} received ${formatUtcTime(first.t)} to ${formatUtcTime(last.t)}, scaled to its own min and max`
      : "TE, no samples";
  return (
    <div className="spark">
      <svg
        className="spark-svg"
        viewBox={`0 0 ${W} ${H}`}
        width={W}
        height={H}
        role="img"
        aria-label={label}
      >
        {pts.length === 0 ? (
          <line
            className="spark-empty"
            x1="1"
            x2={W - 1}
            y1={H / 2}
            y2={H / 2}
          />
        ) : pts.length === 1 ? (
          <circle className="spark-dot" cx={pts[0]?.x} cy={pts[0]?.y} r="2" />
        ) : (
          <polyline className="spark-line" points={pointsAttr(pts)} />
        )}
      </svg>
      <span className="spark-label">
        TE ·{" "}
        {first && last
          ? `${s.length} received ${formatUtcTime(first.t)}–${formatUtcTime(last.t)}`
          : MISSING}{" "}
        · own min–max scale
      </span>
    </div>
  );
}

function Row({
  label,
  field,
  children,
}: {
  label: string;
  field: string;
  children: string;
}) {
  return (
    <div className="trow">
      <dt title={field}>{label}</dt>
      <dd className="num tval">{children}</dd>
    </div>
  );
}

function TrackerCard({
  id,
  m,
  entry,
  ring,
  now,
}: {
  id: string;
  m: TrackerAnalytics;
  entry: Entry;
  ring: readonly Entry[] | undefined;
  now: number;
}) {
  const conn = useBus((s) => s.conn);
  const fresh = freshness(SUBJECTS.tracker, entry, conn, now);
  const span = windowSpanMs(m);
  const hid = `p-tracker-${id}`;
  return (
    <section
      className="panel tcard"
      aria-labelledby={hid}
      data-fresh={fresh.state === "stale" ? "stale" : "ok"}
    >
      <PanelHead
        id={hid}
        title={
          <>
            book {m.bookId}
            {" vs "}
            <WireText value={m.benchmark?.symbol || MISSING} />{" "}
            <span className="tkind">{kindLabel(m.kind)}</span>
          </>
        }
        note={
          TRACKER_BASIS_IS_DEMO ? (
            <span className="demo-badge" title={DEMO_TITLE}>
              DEMO
            </span>
          ) : undefined
        }
        fresh={fresh}
        ageLabel="updated"
      />
      <dl className="tlist">
        <div className="trow trow-kpi">
          <dt title="tracking_error_ann_e9">Tracking error (ann.)</dt>
          <dd className="num tval kpi">
            {formatBpsE9(m.trackingErrorAnnE9)}{" "}
            <span className="unit">bps</span>
          </dd>
        </div>
        <Row label="Tracking difference" field="tracking_diff_e9">
          {`${formatBpsE9(m.trackingDiffE9)} bps`}
        </Row>
        <Row label="Cash drag" field="cash_drag_e9">
          {`${formatBpsE9(m.cashDragE9)} bps`}
        </Row>
        <Row label="Cash weight" field="cash_weight_e9">
          {`${formatPctE9(m.cashWeightE9)} %`}
        </Row>
      </dl>
      <Sparkline ring={ring} />
      <p className="tmeta num">
        <span title="n_obs">n_obs {m.nObs}</span>
        {" · "}
        <span title="sampling_interval_s">
          sampling_interval_s {m.samplingIntervalS}
        </span>
        {" · "}
        <span title="window_end_ns - window_start_ns, Delta One feed clock">
          window {span === null ? MISSING : formatAge(span)}
        </span>
        {fresh.state === "stale" ? (
          <>
            {" · "}last message {formatUtcTime(entry.receivedAtMs)}
          </>
        ) : null}
      </p>
      <p className="tnote">{PRICE_ONLY_NOTE}</p>
    </section>
  );
}

/** Tracker scorecard (ui/CLAUDE.md screen 1, own screen per DESIGN.md Open questions 6): one card per d1.tracker.<book>. Values only. */
export function TrackerBoard({ now }: { now: number }) {
  const conn = useBus((s) => s.conn);
  const st = useBus((s) => s.subjects.tracker);
  const cards = useMemo(
    () =>
      Object.entries(st.latest)
        .flatMap(([key, entry]) => {
          const m = asTracker(entry.value);
          return m ? [{ key, m, entry }] : [];
        })
        .sort((a, b) => a.m.bookId - b.m.bookId),
    [st.latest],
  );
  const fresh = freshness(SUBJECTS.tracker, st.last, conn, now);
  return (
    <section className="tracker" aria-labelledby="p-tracker">
      <h2 id="p-tracker" className="tracker-title">
        Tracker scorecard <code>d1.tracker.&lt;book&gt;</code>
      </h2>
      {cards.length === 0 ? (
        <section className="panel" aria-label="Tracker waiting">
          <PanelHead id="p-tracker-wait" title="Tracker books" fresh={fresh} />
          <WaitingBody
            what={<code>d1.tracker.*</code>}
            extra="The bus does not list which books are tracker books, so none can be shown as waiting individually."
          />
        </section>
      ) : (
        <div className="tgrid">
          {cards.map((c) => (
            <TrackerCard
              key={c.key}
              id={c.key}
              m={c.m}
              entry={c.entry}
              ring={st.rings[c.key]}
              now={now}
            />
          ))}
        </div>
      )}
    </section>
  );
}
