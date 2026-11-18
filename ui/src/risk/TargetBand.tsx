import { useMemo } from "react";
import { freshness } from "../bus/freshness";
import { SUBJECTS } from "../bus/subjects";
import { Instrument } from "../components/Instrument";
import { StateChip } from "../components/StateChip";
import { XScroll } from "../components/XScroll";
import { MISSING, formatAge, formatQtyE2 } from "../format";
import { useBus } from "../store";
import { gapQtyE2, insideBand, joinTargetsPositions } from "./model";
import { PanelHead, WaitingBody } from "./parts";

const q = (v: bigint | null) => (v === null ? MISSING : formatQtyE2(v));

export function TargetBand({ now }: { now: number }) {
  const conn = useBus((s) => s.conn);
  const targets = useBus((s) => s.subjects.targets);
  const positions = useBus((s) => s.subjects.positions);
  const rows = useMemo(
    () => joinTargetsPositions(targets.latest, positions.latest),
    [targets.latest, positions.latest],
  );
  // Header state is the positions feed (1 s cadence). Targets are event-driven: per-row age, never stale.
  const fresh = freshness(SUBJECTS.positions, positions.last, conn, now);
  return (
    <section className="panel" aria-labelledby="p-band">
      <PanelHead
        id="p-band"
        title="Target / position / in-flight"
        hint="exo.targets.*.* and d1.positions.*. Band = EXO's no-trade half-width (ADR-014 s11). Targets are event-driven: an age, never stale."
        fresh={fresh}
      />
      {targets.count === 0 && positions.count === 0 ? (
        <WaitingBody
          what={
            <>
              <code>exo.targets.*.*</code> or <code>d1.positions.*</code>
            </>
          }
        />
      ) : (
        <XScroll label="Target, position and in-flight table">
          <table className="grid" aria-labelledby="p-band">
            <thead>
              <tr>
                <th scope="col">Instrument</th>
                <th scope="col">Book</th>
                <th scope="col" className="r">
                  Target
                </th>
                <th scope="col" className="r">
                  Position
                </th>
                <th scope="col" className="r">
                  In-flight
                </th>
                <th scope="col" className="r">
                  Gap
                </th>
                <th scope="col" className="r">
                  Band
                </th>
                <th scope="col">Gap vs band</th>
                <th scope="col" className="r">
                  Target age
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const gap = gapQtyE2(r.target, r.position);
                const inside = insideBand(gap, r.band);
                const pf = freshness(
                  SUBJECTS.positions,
                  positions.latest[String(r.bookId)],
                  conn,
                  now,
                );
                const stale = pf.state === "stale";
                return (
                  <tr key={`${r.bookId}:${r.instrumentId}`}>
                    <th scope="row">
                      <Instrument id={r.instrumentId} />
                    </th>
                    <td className="num">book {r.bookId}</td>
                    <td className="num r">
                      {r.target === null ? (
                        <span className="na">no target yet</span>
                      ) : (
                        formatQtyE2(r.target)
                      )}
                    </td>
                    <td className="num r" data-fresh={stale ? "stale" : "ok"}>
                      {stale ? (
                        <>
                          <StateChip
                            state="stale"
                            clock={
                              pf.ageMs === null ? null : formatAge(pf.ageMs)
                            }
                          />{" "}
                        </>
                      ) : null}
                      {q(r.position)}
                    </td>
                    <td className="num r">{q(r.inflight)}</td>
                    <td className="num r">{q(gap)}</td>
                    <td className="num r">{q(r.band)}</td>
                    <td>
                      {inside === null
                        ? MISSING
                        : inside
                          ? "inside band"
                          : "outside band"}
                    </td>
                    <td className="num r">
                      {r.targetAtMs === null
                        ? MISSING
                        : formatAge(Math.max(0, now - r.targetAtMs))}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </XScroll>
      )}
      <p className="foot">
        Gap = target − position, in-flight not netted. Band = no-trade
        half-width. – = not reported yet.
      </p>
    </section>
  );
}
