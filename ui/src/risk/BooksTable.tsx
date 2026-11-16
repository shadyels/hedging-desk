import { useMemo } from "react";
import { freshness } from "../bus/freshness";
import { SUBJECTS } from "../bus/subjects";
import { StateChip } from "../components/StateChip";
import { WireText } from "../components/WireText";
import { MISSING, formatAge, formatE9, formatQtyE2 } from "../format";
import { useBus } from "../store";
import { valuationRows } from "./model";
import { PanelHead, WaitingBody } from "./parts";

const money0 = (v: bigint) => formatE9(v, 0);

export function BooksTable({ now }: { now: number }) {
  const conn = useBus((s) => s.conn);
  const st = useBus((s) => s.subjects.valuations);
  const rows = useMemo(() => valuationRows(st.latest), [st.latest]);
  // Panel header: the whole subject (any book). Per-row chips: that book's own age.
  const fresh = freshness(SUBJECTS.valuations, st.last, conn, now);
  return (
    <section className="panel" aria-labelledby="p-books">
      <PanelHead
        id="p-books"
        title="Books"
        note={<code>exo.valuations.&lt;book&gt;</code>}
        fresh={fresh}
      />
      {st.count === 0 ? (
        <WaitingBody what={<code>exo.valuations.*</code>} />
      ) : (
        <table className="grid" aria-labelledby="p-books">
          <thead>
            <tr>
              <th scope="col">Book</th>
              <th scope="col">State</th>
              <th scope="col">Underlying</th>
              <th scope="col">Model</th>
              <th scope="col" className="r">
                Paths
              </th>
              <th scope="col" className="r">
                Seed
              </th>
              <th scope="col" className="r">
                Delta qty
              </th>
              <th scope="col" className="r">
                Vega
              </th>
              <th scope="col" className="r">
                Gamma
              </th>
              <th scope="col" className="r">
                Rho
              </th>
              <th scope="col" className="r">
                Theta
              </th>
              <th scope="col" className="r">
                Div sens
              </th>
              <th scope="col">Std error</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const f = freshness(
                SUBJECTS.valuations,
                st.latest[String(r.bookId)],
                conn,
                now,
              );
              const stale = f.state === "stale";
              const dash = (s: string) => (r.hasLines ? s : MISSING);
              return (
                <tr
                  key={`${r.bookId}:${r.instrumentId}`}
                  data-fresh={stale ? "stale" : "ok"}
                >
                  <th scope="row" className="num">
                    book {r.bookId}
                  </th>
                  <td>
                    {stale ? (
                      <StateChip
                        state="stale"
                        clock={f.ageMs === null ? null : formatAge(f.ageMs)}
                      />
                    ) : (
                      <>
                        <StateChip state="ok" label="OK" />{" "}
                        <span className="num age">
                          {formatAge(Math.max(0, now - r.receivedAtMs))}
                        </span>
                      </>
                    )}
                  </td>
                  <td>
                    {r.hasLines ? (
                      <WireText value={r.symbol || MISSING} />
                    ) : (
                      <span className="na">no product lines</span>
                    )}
                  </td>
                  <td>
                    <WireText value={r.model || MISSING} />
                  </td>
                  <td className="num r">{r.nPaths.toLocaleString("en-US")}</td>
                  <td className="num r">{r.seed.toString()}</td>
                  <td className="num r">{dash(formatQtyE2(r.delta))}</td>
                  <td className="num r">{dash(money0(r.vega))}</td>
                  <td className="num r">{dash(formatQtyE2(r.gamma))}</td>
                  <td className="num r">{dash(money0(r.rho))}</td>
                  <td className="num r">{dash(money0(r.theta))}</td>
                  <td className="num r">{dash(money0(r.divSens))}</td>
                  <td>
                    {r.flagged.length > 0 ? (
                      <span className="se-flag">
                        <span aria-hidden="true">*</span> high SE:{" "}
                        <WireText value={r.flagged.join(", ")} />
                      </span>
                    ) : null}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      <p className="foot">
        One row per book and underlying: lines sum per underlying (ADR-014 s9),
        never across underlyings. Delta in underlying units, others in
        underlying currency. * high SE = MC std error above config max for the
        named Greeks. Book codes are not on the bus; rows show the book id.
      </p>
    </section>
  );
}
