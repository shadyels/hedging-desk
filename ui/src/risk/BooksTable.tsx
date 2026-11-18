import { useMemo } from "react";
import { freshness, type Freshness } from "../bus/freshness";
import { SUBJECTS } from "../bus/subjects";
import { StateChip } from "../components/StateChip";
import { WireText } from "../components/WireText";
import {
  MISSING,
  formatAge,
  formatE9,
  formatQtyE2,
  formatUtcTime,
} from "../format";
import { asHealth, parseD1, parseExo } from "../health/model";
import { useBus } from "../store";
import { bookViews, valuationRows, type BookView } from "./model";
import { NoneSince, PanelHead, WaitingBody } from "./parts";

const money0 = (v: bigint) => formatE9(v, 0);

/**
 * One chip per book row. Precedence (DESIGN.md), disconnected being board-wide (greyscale): halted, then
 * waiting or stale (exclusive: stale needs a valuation), then OK. A book that exo.health or a recon alert
 * names as halted has reported, so HALTED shows even before its first valuation (deliberate: hiding a halt
 * behind WAITING is worse than the strict ladder). Outranked states go in the tooltip.
 */
function BookChip({
  v,
  f,
  receivedAtMs,
  now,
}: {
  v: BookView;
  f: Freshness;
  receivedAtMs: number | null;
  now: number;
}) {
  const age = f.ageMs === null ? null : formatAge(f.ageMs);
  const a = v.reconAlert;
  if (v.haltReasons.length > 0) {
    const also = [
      f.state === "stale" ? `also STALE ${age ?? ""}`.trim() : null,
      v.rows.length === 0 ? "no valuation yet" : null,
      a
        ? `recon: EXO view ${formatQtyE2(a.exoViewQtyE2)}, D1 view ${formatQtyE2(a.d1ViewQtyE2)}`
        : null,
    ].filter((t) => t !== null);
    return (
      <span title={also.length > 0 ? also.join("; ") : undefined}>
        <StateChip
          state="halted"
          label={`HALTED ${v.haltReasons.join(", ")}`}
        />
      </span>
    );
  }
  if (receivedAtMs === null)
    return (
      <StateChip state="waiting" clock={f.state === "waiting" ? age : null} />
    );
  if (f.state === "stale") return <StateChip state="stale" clock={age} />;
  return (
    <>
      <StateChip state="ok" label="OK" />{" "}
      <span className="num age">
        {formatAge(Math.max(0, now - receivedAtMs))}
      </span>
    </>
  );
}

export function BooksTable({ now }: { now: number }) {
  const conn = useBus((s) => s.conn);
  const st = useBus((s) => s.subjects.valuations);
  const exoLast = useBus((s) => s.subjects.exoHealth.last);
  const d1Last = useBus((s) => s.subjects.d1Health.last);
  const reconSt = useBus((s) => s.subjects.reconAlerts);
  const views = useMemo(() => {
    const exoMsg = asHealth(exoLast?.value);
    const d1Msg = asHealth(d1Last?.value);
    return bookViews(
      valuationRows(st.latest),
      exoMsg ? parseExo(exoMsg) : null,
      d1Msg ? parseD1(d1Msg) : null,
      reconSt.rings[""] ?? [],
    );
  }, [st.latest, exoLast, d1Last, reconSt.rings]);
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
      {views.length === 0 ? (
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
            {views.flatMap((v) =>
              // A book with no valuation yet gets one placeholder row: WAITING, no number (DESIGN.md "waiting").
              (v.rows.length > 0 ? v.rows : [null]).map((r) => {
                const entry = st.latest[String(v.bookId)];
                const f = freshness(SUBJECTS.valuations, entry, conn, now);
                const dash = (x: string) => (r?.hasLines ? x : MISSING);
                const state =
                  v.haltReasons.length > 0
                    ? "halted"
                    : f.state === "stale"
                      ? "stale"
                      : "ok";
                return (
                  <tr
                    key={`${v.bookId}:${r?.instrumentId ?? "none"}`}
                    data-fresh={state}
                  >
                    <th scope="row" className="num">
                      book {v.bookId}
                    </th>
                    <td>
                      <BookChip
                        v={v}
                        f={f}
                        receivedAtMs={r ? r.receivedAtMs : null}
                        now={now}
                      />
                    </td>
                    <td>
                      {r === null ? (
                        <span className="na">
                          no valuation yet
                          {v.exoState === undefined ? null : (
                            <>
                              {" · "}
                              <WireText value={v.exoState} />
                            </>
                          )}
                        </span>
                      ) : r.hasLines ? (
                        <WireText value={r.symbol || MISSING} />
                      ) : (
                        <span className="na">no product lines</span>
                      )}
                    </td>
                    <td>
                      <WireText value={r?.model || MISSING} />
                    </td>
                    <td className="num r">
                      {r ? r.nPaths.toLocaleString("en-US") : MISSING}
                    </td>
                    <td className="num r">{r ? r.seed.toString() : MISSING}</td>
                    <td className="num r">
                      {r ? dash(formatQtyE2(r.delta)) : MISSING}
                    </td>
                    <td className="num r">
                      {r ? dash(money0(r.vega)) : MISSING}
                    </td>
                    <td className="num r">
                      {r ? dash(formatQtyE2(r.gamma)) : MISSING}
                    </td>
                    <td className="num r">
                      {r ? dash(money0(r.rho)) : MISSING}
                    </td>
                    <td className="num r">
                      {r ? dash(money0(r.theta)) : MISSING}
                    </td>
                    <td className="num r">
                      {r ? dash(money0(r.divSens)) : MISSING}
                    </td>
                    <td>
                      {r && r.flagged.length > 0 ? (
                        <span className="se-flag">
                          <span aria-hidden="true">*</span> high SE:{" "}
                          <WireText value={r.flagged.join(", ")} />
                        </span>
                      ) : null}
                    </td>
                  </tr>
                );
              }),
            )}
          </tbody>
        </table>
      )}
      <p className="foot">
        <code>exo.alerts.recon</code>:{" "}
        {reconSt.count === 0 || reconSt.last === null ? (
          <NoneSince />
        ) : (
          `${reconSt.count} since connect, last ${formatUtcTime(reconSt.last.receivedAtMs)}`
        )}
        . HALTED comes from exo.health book state or a recon alert; Delta One
        publishes no halt (ADR-014 Amendment 1 s2).
      </p>
      <p className="foot">
        One row per book and underlying: lines sum per underlying (ADR-014 s9),
        never across underlyings. Delta in underlying units, others in
        underlying currency. * high SE = MC std error above config max for the
        named Greeks. A book that has not published a valuation reads WAITING.
        Book codes are not on the bus; rows show the book id.
      </p>
    </section>
  );
}
