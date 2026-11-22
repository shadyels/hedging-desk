import { useMemo, type ReactNode } from "react";
import { freshness } from "../bus/freshness";
import { SUBJECTS } from "../bus/subjects";
import { StateChip } from "../components/StateChip";
import { WireText } from "../components/WireText";
import { XScroll } from "../components/XScroll";
import { MISSING, formatFixed, formatUtcTime } from "../format";
import { PanelHead, WaitingBody } from "../risk/parts";
import { asValuation } from "../risk/model";
import { useBus } from "../store";
import {
  asHealth,
  checkGitSha,
  cueFor,
  NOT_PUBLISHED,
  okCue,
  parseD1,
  parseExo,
  parseU64,
  type Cue,
  type Pair,
  type TokenKind,
} from "./model";

/** A cue as a chip, or a plain outlined label for the neutral tone (no hue, not a fault). */
function CueView({ cue }: { cue: Cue }) {
  return cue.tone === "neutral" ? (
    <span className="ntag">{cue.label}</span>
  ) : (
    <StateChip state={cue.tone} label={cue.label} />
  );
}

/** A decimal u64 from the wire. Valid -> grouped bigint text. Invalid -> an error shown as text, never a zero. */
function U64({ raw }: { raw: string }) {
  const p = parseU64(raw);
  return p.ok ? (
    <span className="num">{formatFixed(p.value, 0, 0)}</span>
  ) : (
    <span className="bad">
      <StateChip state="hard" label="NOT A u64" /> <WireText value={raw} />
    </span>
  );
}

/** Nanoseconds since the epoch -> UTC clock. 0 = never. A value beyond the Date range is shown as an error, not clamped. */
const MAX_DATE_MS = 8_640_000_000_000_000n;
function NsTime({ ns }: { ns: bigint }) {
  if (ns === 0n) return <>{MISSING}</>;
  const ms = ns / 1_000_000n;
  return ms > MAX_DATE_MS ? (
    <span className="bad">out of range</span>
  ) : (
    <span className="num">{formatUtcTime(Number(ms))}</span>
  );
}
function NsTimeText({ raw }: { raw: string | undefined }) {
  if (raw === undefined) return <>{MISSING}</>;
  const p = parseU64(raw);
  return p.ok ? (
    <NsTime ns={p.value} />
  ) : (
    <span className="bad">
      <StateChip state="hard" label="NOT A u64" /> <WireText value={raw} />
    </span>
  );
}

/** Published value, verbatim. Absent = a dash, never a placeholder zero. */
const Raw = ({ v }: { v: string | undefined }) =>
  v === undefined ? (
    <span className="na">{MISSING}</span>
  ) : (
    <WireText value={v} />
  );

function TokenRow({
  label,
  field,
  kind,
  raw,
}: {
  label: string;
  field: string;
  kind: TokenKind;
  raw: string | undefined;
}) {
  return (
    <tr>
      <th scope="row">{label}</th>
      <td>
        <CueView cue={cueFor(kind, raw)} />
      </td>
      <td>
        <code>{field}</code> = <Raw v={raw} />
      </td>
    </tr>
  );
}

function CounterRow({
  label,
  field,
  raw,
}: {
  label: string;
  field: string;
  raw: string | undefined;
}) {
  return (
    <tr>
      <th scope="row">{label}</th>
      <td>
        {raw === undefined ? (
          <CueView cue={NOT_PUBLISHED} />
        ) : (
          <span className="ntag">COUNTER</span>
        )}
      </td>
      <td>
        <code>{field}</code> ={" "}
        {raw === undefined ? <Raw v={raw} /> : <U64 raw={raw} />}
      </td>
    </tr>
  );
}

/** Detail keys outside the contract, verbatim. Key and value are wire text: text nodes only, index as React key. */
function OtherKeys({ pairs }: { pairs: readonly Pair[] }) {
  if (pairs.length === 0) return null;
  return (
    <>
      <h4 className="sub">Other detail keys (not in the contract)</h4>
      <XScroll label="Other detail keys table">
        {" "}
        <table className="grid" aria-label="Other detail keys">
          <tbody>
            {pairs.map(([k, v], i) => (
              <tr key={i}>
                <th scope="row">
                  <WireText value={k} />
                </th>
                <td>
                  <WireText value={v} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </XScroll>
    </>
  );
}

const staleAttr = (state: string) => (state === "stale" ? "stale" : "ok");

// ---- Delta One ------------------------------------------------------------------------------

function D1Panels({ now }: { now: number }) {
  const conn = useBus((s) => s.conn);
  const st = useBus((s) => s.subjects.d1Health);
  const fresh = freshness(SUBJECTS.d1Health, st.last, conn, now);
  const h = useMemo(() => {
    const m = asHealth(st.last?.value);
    return m ? parseD1(m) : null;
  }, [st.last]);
  return (
    <div className="health-pair">
      <section
        className="panel"
        aria-labelledby="p-d1"
        data-fresh={staleAttr(fresh.state)}
      >
        <PanelHead
          id="p-d1"
          title="Delta One"
          hint="d1.health: detail keys per ADR-014 Amendment 1 s2"
          fresh={fresh}
          ageLabel="heartbeat"
        />
        {h === null ? (
          <WaitingBody what={<code>d1.health</code>} />
        ) : (
          <>
            <XScroll label="Delta One checks table">
              {" "}
              <table className="grid" aria-labelledby="p-d1">
                <thead>
                  <tr>
                    <th scope="col">Check</th>
                    <th scope="col">State</th>
                    <th scope="col">Published</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <th scope="row">Overall</th>
                    <td>
                      <CueView cue={okCue(h.ok)} />
                    </td>
                    <td>
                      <code>ok</code> = {h.ok ? "true" : "false"}, component{" "}
                      <WireText value={h.component} />
                    </td>
                  </tr>
                  <TokenRow
                    label="NATS"
                    field="nats"
                    kind="nats"
                    raw={h.nats}
                  />
                  <TokenRow
                    label="FIX session"
                    field="fix.session"
                    kind="fix"
                    raw={h.fix}
                  />
                  <TokenRow
                    label="Post-trade"
                    field="posttrade"
                    kind="posttrade"
                    raw={h.posttrade}
                  />
                  {h.posttrade === "disabled" ? null : (
                    <CounterRow
                      label="Post-trade send failures"
                      field="posttrade.send_failures"
                      raw={h.sendFailures}
                    />
                  )}
                  <CounterRow
                    label="Unattributable rejects"
                    field="limits.unattributable_rejects"
                    raw={h.unattributable}
                  />
                </tbody>
              </table>
            </XScroll>
            <OtherKeys pairs={h.other} />
          </>
        )}
        <p
          className="foot"
          title="ok is true only when the FIX session is logged on and post-trade is disabled or running. Limit levels never change it (ADR-014 A1 s2). d1.health has no halted or kill-switch field: that state arrives with the P3.M2 commands."
        >
          Overall is Delta One&apos;s own ok flag.
        </p>
      </section>

      <section
        className="panel"
        aria-labelledby="p-d1-books"
        data-fresh={staleAttr(fresh.state)}
      >
        <PanelHead
          id="p-d1-books"
          title="Books (Delta One view)"
          hint="d1.health limits.book.<id>.*: Delta One's view of each book's latest EXO snapshot"
          fresh={fresh}
          ageLabel="heartbeat"
        />
        {h === null ? (
          <WaitingBody what={<code>d1.health</code>} />
        ) : h.books.length === 0 ? (
          <p className="empty">
            d1.health carries no <code>limits.book.&lt;id&gt;.*</code> keys: no
            book is configured, or none is published.
          </p>
        ) : (
          <XScroll label="Delta One books table">
            {" "}
            <table className="grid" aria-labelledby="p-d1-books">
              <thead>
                <tr>
                  <th scope="col">Book</th>
                  <th scope="col">Valuation</th>
                  <th scope="col">Published</th>
                  <th scope="col" className="r">
                    Rejects
                  </th>
                </tr>
              </thead>
              <tbody>
                {h.books.map((b) => (
                  <tr key={b.id}>
                    <th scope="row" className="num">
                      book {b.id}
                    </th>
                    <td>
                      <CueView cue={cueFor("valuation", b.valuation)} />
                    </td>
                    <td>
                      <Raw v={b.valuation} />
                    </td>
                    <td className="num r">
                      {b.rejects === undefined ? (
                        <span className="na">{MISSING}</span>
                      ) : (
                        <U64 raw={b.rejects} />
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </XScroll>
        )}
        <p
          className="foot"
          title="STALE and INVALID force a WARN on the book's limits (ADR-014 s3). Book codes are not on the bus."
        >
          NONE = never valued and never rejected: normal, not a fault.
        </p>
      </section>
    </div>
  );
}

// ---- EXO ------------------------------------------------------------------------------------

function ExoPanel({ now }: { now: number }) {
  const conn = useBus((s) => s.conn);
  const st = useBus((s) => s.subjects.exoHealth);
  const fresh = freshness(SUBJECTS.exoHealth, st.last, conn, now);
  const h = useMemo(() => {
    const m = asHealth(st.last?.value);
    return m ? parseExo(m) : null;
  }, [st.last]);
  return (
    <section
      className="panel"
      aria-labelledby="p-exo"
      data-fresh={staleAttr(fresh.state)}
    >
      <PanelHead
        id="p-exo"
        title="EXO"
        hint="exo.health: detail keys per ADR-014 s12"
        fresh={fresh}
        ageLabel="heartbeat"
      />
      {h === null ? (
        <WaitingBody what={<code>exo.health</code>} />
      ) : (
        <>
          <XScroll label="EXO checks table">
            {" "}
            <table className="grid" aria-labelledby="p-exo">
              <thead>
                <tr>
                  <th scope="col">Check</th>
                  <th scope="col">State</th>
                  <th scope="col">Published</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <th scope="row">Overall</th>
                  <td>
                    <CueView cue={okCue(h.ok)} />
                  </td>
                  <td>
                    <code>ok</code> = {h.ok ? "true" : "false"}, component{" "}
                    <WireText value={h.component} />
                  </td>
                </tr>
                <tr>
                  <th scope="row">git_sha</th>
                  <td>
                    <span className="ntag">VERSION</span>
                  </td>
                  <td>
                    <code>git_sha</code> = <Raw v={h.gitSha} />
                  </td>
                </tr>
                <TokenRow label="NATS" field="nats" kind="nats" raw={h.nats} />
                <TokenRow
                  label="Publishing"
                  field="publish"
                  kind="publish"
                  raw={h.publish}
                />
                <TokenRow
                  label="Valuation"
                  field="valuation_in_flight"
                  kind="inFlight"
                  raw={h.inFlight}
                />
                <CounterRow
                  label="Rejected inbound"
                  field="rejected_total"
                  raw={h.rejectedTotal}
                />
                {h.fatal === undefined ? null : (
                  <tr>
                    <th scope="row">Fatal</th>
                    <td>
                      <CueView cue={okCue(false)} />
                    </td>
                    <td>
                      <code>fatal</code> = <WireText value={h.fatal} />
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </XScroll>

          <h4 className="sub">Book state (EXO)</h4>
          {h.books.length === 0 ? (
            <p className="empty">
              exo.health carries no <code>book.&lt;id&gt;.*</code> keys.
            </p>
          ) : (
            <XScroll label="EXO book state table">
              {" "}
              <table className="grid" aria-label="EXO book state">
                <thead>
                  <tr>
                    <th scope="col">Book</th>
                    <th scope="col">State</th>
                    <th scope="col">Published</th>
                    <th scope="col">Last valuation</th>
                    <th scope="col">Last error</th>
                  </tr>
                </thead>
                <tbody>
                  {h.books.map((b) => (
                    <tr key={b.id}>
                      <th scope="row" className="num">
                        book {b.id}
                      </th>
                      <td>
                        <CueView cue={cueFor("exoBook", b.state)} />
                      </td>
                      <td>
                        <Raw v={b.state} />
                      </td>
                      <td>
                        <NsTimeText raw={b.lastValuationNs} />
                      </td>
                      <td>
                        <Raw v={b.lastError} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </XScroll>
          )}

          <h4 className="sub">Limit levels reported by EXO</h4>
          {h.limits.length === 0 ? (
            <p className="empty">
              No <code>limit.*</code> keys: no limit alert is currently raised,
              or EXO has not seen one. An absent key means OK or unknown.
            </p>
          ) : (
            <XScroll label="EXO limit levels table">
              {" "}
              <table className="grid" aria-label="EXO limit levels">
                <thead>
                  <tr>
                    <th scope="col">Instrument id</th>
                    <th scope="col">Greek</th>
                    <th scope="col">Level</th>
                    <th scope="col">Published</th>
                  </tr>
                </thead>
                <tbody>
                  {h.limits.map((l) => (
                    <tr key={`${l.instrumentId}.${l.greek}`}>
                      <th scope="row" className="num">
                        {l.instrumentId}
                      </th>
                      <td>{l.greek}</td>
                      <td>
                        <CueView cue={cueFor("limit", l.level)} />
                      </td>
                      <td>
                        <WireText value={l.level} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </XScroll>
          )}
          <OtherKeys pairs={h.other} />
        </>
      )}
      <p
        className="foot"
        title="ok is false when EXO is disconnected, refusing to publish, has a book halted, or a book's latest valuation failed (ADR-014 s12). The limit source is d1.alerts.limits."
      >
        Limit levels here are EXO&apos;s copy of the latest non-cleared alert.
      </p>
    </section>
  );
}

// ---- Valuation metadata and provenance ------------------------------------------------------

function MetadataPanel({ now }: { now: number }) {
  const conn = useBus((s) => s.conn);
  const st = useBus((s) => s.subjects.valuations);
  const exo = useBus((s) => s.subjects.exoHealth.last);
  const fresh = freshness(SUBJECTS.valuations, st.last, conn, now);
  const exoSha = useMemo(() => {
    const m = asHealth(exo?.value);
    return m ? parseExo(m).gitSha : undefined;
  }, [exo]);
  const rows = useMemo(
    () =>
      Object.values(st.latest)
        .flatMap((e) => {
          const v = asValuation(e.value);
          return v ? [{ v, e }] : [];
        })
        .sort((a, b) => a.v.bookId - b.v.bookId),
    [st.latest],
  );
  const check = checkGitSha(
    exoSha,
    rows.flatMap(({ v }) =>
      v.valuation ? [{ bookId: v.bookId, sha: v.valuation.gitSha }] : [],
    ),
  );
  let strip: ReactNode = null;
  if (check.status === "mismatch")
    strip = (
      <div className="hbanner" role="status">
        <StateChip state="warn" label="WARN" /> git_sha mismatch: valuations and
        exo.health do not come from one build.
        <ul>
          {check.sources.map((s, i) => (
            <li key={i}>
              {s.label} = <WireText value={s.sha} />
            </li>
          ))}
        </ul>
      </div>
    );
  else if (check.status === "consistent")
    strip = (
      <p className="hline">
        <StateChip state="ok" label="OK" /> all {check.count} git_sha values
        (exo.health and valuations) are <WireText value={check.sha} />
      </p>
    );
  else
    strip = (
      <p className="hline">
        <span className="ntag">NOT COMPARED</span> need two git_sha values to
        compare, have {check.count}.
      </p>
    );
  return (
    <section
      className="panel"
      aria-labelledby="p-meta"
      data-fresh={staleAttr(fresh.state)}
    >
      <PanelHead
        id="p-meta"
        title="Valuation metadata"
        hint="exo.valuations.<book>: model, params, seed, n_paths and git_sha make a number reproducible (root invariant 7)"
        fresh={fresh}
      />
      {rows.length === 0 ? (
        <WaitingBody what={<code>exo.valuations.*</code>} />
      ) : (
        <>
          {strip}
          <XScroll label="Valuation metadata table">
            {" "}
            <table className="grid" aria-labelledby="p-meta">
              <thead>
                <tr>
                  <th scope="col">Book</th>
                  <th scope="col">Model</th>
                  <th scope="col" className="r">
                    Seed
                  </th>
                  <th scope="col" className="r">
                    n_paths
                  </th>
                  <th scope="col">git_sha</th>
                  <th scope="col">params_hash</th>
                  <th scope="col">As of (UTC)</th>
                </tr>
              </thead>
              <tbody>
                {rows.map(({ v, e }, i) => {
                  const f = freshness(SUBJECTS.valuations, e, conn, now);
                  const m = v.valuation;
                  return (
                    <tr key={i} data-fresh={staleAttr(f.state)}>
                      <th scope="row" className="num">
                        book {v.bookId}
                      </th>
                      {m === undefined ? (
                        <td colSpan={6}>
                          <StateChip state="hard" label="NO METADATA" /> this
                          valuation carries no ValuationMeta, so it is not
                          reproducible
                        </td>
                      ) : (
                        <>
                          <td>
                            <Raw v={m.modelId === "" ? undefined : m.modelId} />
                          </td>
                          <td className="num r">{m.seed.toString()}</td>
                          <td className="num r">
                            {formatFixed(BigInt(m.nPaths), 0, 0)}
                          </td>
                          <td>
                            <Raw v={m.gitSha === "" ? undefined : m.gitSha} />
                          </td>
                          <td className="hash">
                            <Raw
                              v={m.paramsHash === "" ? undefined : m.paramsHash}
                            />
                          </td>
                          <td>
                            <NsTime ns={v.asOfNs} />
                          </td>
                        </>
                      )}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </XScroll>
        </>
      )}
      <p
        className="foot"
        title="d1.risk.firm carries no git_sha, so Delta One is not part of this check. A mismatch can be transient around an EXO restart on a new commit, until each book is revalued."
      >
        git_sha is compared verbatim, &quot;-dirty&quot; included.
      </p>
    </section>
  );
}

export function HealthBoard({ now }: { now: number }) {
  return (
    <div className="health">
      <D1Panels now={now} />
      <ExoPanel now={now} />
      <MetadataPanel now={now} />
    </div>
  );
}
