import { useMemo } from "react";
import { freshness, type Freshness } from "../bus/freshness";
import { SUBJECTS } from "../bus/subjects";
import { WireText } from "../components/WireText";
import { XScroll } from "../components/XScroll";
import { MISSING, formatE9, formatQtyE2 } from "../format";
import type {
  RiskSnapshot_LimitLevel,
  RiskSnapshot_NetGreeks,
} from "../gen/live";
import { useBus } from "../store";
import { headroom, limitChipState, asRisk } from "./model";
import { HeadroomBar, LimitChip, PanelHead, WaitingBody } from "./parts";

const money0 = (v: bigint) => formatE9(v, 0);
const qty2 = (v: bigint) => formatQtyE2(v, 2);

function LimitCell({
  value,
  level,
  warn,
  hard,
  fmt,
  fresh,
}: {
  value: bigint;
  level: RiskSnapshot_LimitLevel;
  warn: bigint;
  hard: bigint;
  fmt: (v: bigint) => string;
  fresh: Freshness;
}) {
  const h = headroom(value, warn, hard);
  const st = limitChipState(level);
  // Tint only a live value (DESIGN.md state matrix): stale and disconnected values stay --text-3.
  const tint =
    fresh.state === "ok" && (st === "warn" || st === "hard") ? st : undefined;
  return (
    <td className="r">
      <span className="limit-cell">
        <span className="num" data-level={tint}>
          {fmt(value)}
        </span>
        <LimitChip level={level} fresh={fresh} />
        {h.kind === "na" ? (
          <span className="na">limit n/a</span>
        ) : (
          <HeadroomBar
            h={h}
            state={limitChipState(level)}
            live={fresh.state !== "stale"}
          />
        )}
      </span>
    </td>
  );
}

function Row({ g, fresh }: { g: RiskSnapshot_NetGreeks; fresh: Freshness }) {
  return (
    <tr>
      <th scope="row">
        <WireText value={g.underlying?.symbol || MISSING} />
        {g.greeksFlagged ? (
          <span
            className="flag"
            title="MC std error above config max (greeks_flagged)"
          >
            *
            <span className="sr-only">
              {" "}
              greeks flagged: MC std error above config max
            </span>
          </span>
        ) : null}
        {g.limitDataSuspect ? (
          <span
            className="suspect"
            title="A contributing book is stale or invalid (ADR-014 s3): limit levels may be forced"
          >
            DATA SUSPECT
          </span>
        ) : null}
      </th>
      <td className="num r">{qty2(g.firmDeltaQtyE2)}</td>
      <td className="num r">{money0(g.notionalE9)}</td>
      <LimitCell
        value={g.vegaE9}
        level={g.vegaLevel}
        warn={g.vegaWarnE9}
        hard={g.vegaHardE9}
        fmt={money0}
        fresh={fresh}
      />
      <LimitCell
        value={g.gammaQtyE2}
        level={g.gammaLevel}
        warn={g.gammaWarnQtyE2}
        hard={g.gammaHardQtyE2}
        fmt={qty2}
        fresh={fresh}
      />
      <LimitCell
        value={g.rhoE9}
        level={g.rhoLevel}
        warn={g.rhoWarnE9}
        hard={g.rhoHardE9}
        fmt={money0}
        fresh={fresh}
      />
      <td className="num r">{money0(g.thetaE9)}</td>
      <td className="num r">{money0(g.divSensE9)}</td>
    </tr>
  );
}

export function FirmTable({ now }: { now: number }) {
  const conn = useBus((s) => s.conn);
  const entry = useBus((s) => s.subjects.riskFirm.last);
  const fresh = freshness(SUBJECTS.riskFirm, entry, conn, now);
  const snap = asRisk(entry?.value);
  const rows = useMemo(
    () =>
      snap
        ? [...snap.greeks].sort(
            (a, b) =>
              (a.underlying?.symbol ?? "").localeCompare(
                b.underlying?.symbol ?? "",
              ) ||
              (a.underlying?.instrumentId ?? 0) -
                (b.underlying?.instrumentId ?? 0),
          )
        : [],
    [snap],
  );
  return (
    <section
      className="panel"
      aria-labelledby="p-firm"
      data-fresh={fresh.state}
    >
      <PanelHead
        id="p-firm"
        title="Firm net Greeks by underlying"
        hint="d1.risk.firm: levels and caps come from Delta One (ADR-014 Amendment 1 s1)"
        fresh={fresh}
      />
      {snap === null ? (
        <WaitingBody what={<code>d1.risk.firm</code>} />
      ) : rows.length === 0 ? (
        <p className="empty">
          d1.risk.firm published an empty snapshot: no underlying has an
          accepted EXO line, a non-zero firm position or a non-OK limit level.
        </p>
      ) : (
        <XScroll label="Firm net Greeks table">
          <table className="grid" aria-labelledby="p-firm">
            <thead>
              <tr>
                <th scope="col">Underlying</th>
                <th scope="col" className="r">
                  Delta qty
                </th>
                <th scope="col" className="r">
                  Notional ccy
                </th>
                <th scope="col" className="r">
                  Vega ccy/vol pt
                </th>
                <th scope="col" className="r">
                  Gamma dDelta/1%
                </th>
                <th scope="col" className="r">
                  Rho ccy/bp
                </th>
                <th scope="col" className="r">
                  Theta ccy/day
                </th>
                <th scope="col" className="r">
                  Div sens ccy/1%
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((g) => (
                <Row
                  key={g.underlying?.instrumentId ?? 0}
                  g={g}
                  fresh={fresh}
                />
              ))}
            </tbody>
          </table>
        </XScroll>
      )}
      <p
        className="foot"
        title="Delta has no limit (ADR-014 s3): it is hedged against EXO targets, see the target panel. Theta and div sens are monitored with no limit. * = greeks_flagged."
      >
        Bar = |value| / hard limit, tick = warn, » = beyond hard. * = MC std
        error above config max. Delta, theta and div sens have no limit.
      </p>
    </section>
  );
}
