import { useVirtualizer } from "@tanstack/react-virtual";
import {
  useCallback,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type UIEvent,
} from "react";
import { freshness } from "../bus/freshness";
import { SUBJECTS } from "../bus/subjects";
import { Instrument } from "../components/Instrument";
import { StateChip } from "../components/StateChip";
import { WireText } from "../components/WireText";
import { XScroll } from "../components/XScroll";
import {
  MISSING,
  formatAge,
  formatPxE9,
  formatQtyE2,
  formatUtcTime,
} from "../format";
import type { InstrumentRef } from "../gen/common";
import { NoneSince, NoneYetBody, PanelHead } from "../risk/parts";
import { useBus } from "../store";
import {
  KINDS,
  NO_FILTER,
  buildBlotter,
  filterOptions,
  flattenFiltered,
  isFiltered,
  sideLabel,
  statusGlyph,
  statusLabel,
  type AllocRow,
  type BlotterFilter,
  type CrossRow,
  type FlatRow,
  type OrderRow,
} from "./model";

/** Fallback when --row-h cannot be read (no stylesheet). The virtualizer needs the row height as a number. */
export const DEFAULT_ROW_H = 24;
/** Rows the virtualizer assumes fit before the scroller is measured (and in jsdom, where nothing is). */
const INITIAL_ROWS = 20;

/** The one row-height token (DESIGN.md "Spacing"), read from CSS so JS never carries its own pixel value. */
function readRowH(): number {
  const v = parseFloat(
    getComputedStyle(document.documentElement).getPropertyValue("--row-h"),
  );
  return v > 0 ? v : DEFAULT_ROW_H;
}
const OVERSCAN = 8;
const PX_DECIMALS = 4;

const px = (v: bigint) => (v > 0n ? formatPxE9(v, PX_DECIMALS) : MISSING);

function Instr({ i }: { i: InstrumentRef | undefined }) {
  if (!i) return <>{MISSING}</>;
  return <Instrument id={i.instrumentId} />;
}

function Cells({
  row,
  collapsed,
  toggle,
}: {
  row: FlatRow;
  collapsed: boolean;
  toggle: (key: string) => void;
}) {
  if (row.kind === "cross") return <CrossCells row={row} />;
  if (row.kind === "alloc") return <AllocCells row={row} />;
  return <OrderCells row={row} collapsed={collapsed} toggle={toggle} />;
}

const Time = ({ ms }: { ms: number }) => (
  <span role="cell" className="c-time">
    {formatUtcTime(ms)}
  </span>
);

function OrderCells({
  row,
  collapsed,
  toggle,
}: {
  row: OrderRow;
  collapsed: boolean;
  toggle: (key: string) => void;
}) {
  const label = statusLabel(row.status);
  const glyph = statusGlyph(row.status);
  return (
    <>
      <span role="cell" className="c-tog">
        {row.allocs.length > 0 ? (
          <button
            type="button"
            className="bl-tog"
            aria-expanded={!collapsed}
            aria-label={`${collapsed ? "Expand" : "Collapse"} ${row.allocs.length} allocation${row.allocs.length === 1 ? "" : "s"} of ${row.clOrdId}`}
            onClick={() => toggle(row.key)}
          >
            <span aria-hidden="true">{collapsed ? "▸" : "▾"}</span>
          </button>
        ) : null}
      </span>
      <Time ms={row.atMs} />
      <span role="cell">ORDER</span>
      <span role="cell" className="c-id">
        <WireText value={row.clOrdId} />
        {row.nettingCycleId !== "" ? " (net)" : null}
      </span>
      <span role="cell">{row.bookId === 0 ? "0 firm" : row.bookId}</span>
      <span role="cell">
        <Instr i={row.instrument} />
      </span>
      <span role="cell">{sideLabel(row.side)}</span>
      <span role="cell" className="r">
        {formatQtyE2(row.qtyE2)}
      </span>
      <span role="cell" className="r">
        {px(row.lastPxE9)}
      </span>
      <span role="cell" className="c-status">
        {glyph ? (
          <>
            <span aria-hidden="true">{glyph}</span>{" "}
          </>
        ) : null}
        {label}
      </span>
      <span role="cell" className="r">
        {formatQtyE2(row.cumQtyE2)}
      </span>
      <span role="cell" className="r">
        {formatQtyE2(row.leavesQtyE2)}
      </span>
      <span role="cell" className="c-detail">
        {row.text !== "" ? <WireText value={row.text} /> : null}
      </span>
    </>
  );
}

/** An allocation report's status, cum and leaves are the PARENT's (proto comment), so none is shown here. */
function AllocCells({ row }: { row: AllocRow }) {
  return (
    <>
      <span role="cell" className="c-tog" />
      <Time ms={row.atMs} />
      <span role="cell">ALLOC</span>
      <span role="cell" className="c-id c-alloc">
        <WireText value={row.clOrdId} />/{row.bookId}
      </span>
      <span role="cell">{row.bookId}</span>
      <span role="cell">
        <Instr i={row.instrument} />
      </span>
      <span role="cell">{sideLabel(row.side)}</span>
      <span role="cell" className="r" title="this book's share (last_qty_e2)">
        {formatQtyE2(row.lastQtyE2)}
      </span>
      <span role="cell" className="r">
        {px(row.lastPxE9)}
      </span>
      <span role="cell">{MISSING}</span>
      <span
        role="cell"
        className="r"
        title="cumulative to this book (book_cum_qty_e2)"
      >
        {formatQtyE2(row.bookCumQtyE2)}
      </span>
      <span role="cell" className="r">
        {MISSING}
      </span>
      <span role="cell" className="c-detail" />
    </>
  );
}

function CrossCells({ row }: { row: CrossRow }) {
  const l = row.link;
  return (
    <>
      <span role="cell" className="c-tog" />
      <Time ms={row.atMs} />
      <span role="cell">CROSS</span>
      <span role="cell" className="c-id c-full">
        <WireText value={row.crossId} />
      </span>
      <span role="cell" title="buy book, sell book">
        buy {row.buyBookId} · sell {row.sellBookId}
      </span>
      <span role="cell">
        <Instr i={row.instrument} />
      </span>
      <span role="cell">{MISSING}</span>
      <span role="cell" className="r">
        {formatQtyE2(row.qtyE2)}
      </span>
      <span role="cell" className="r">
        {px(row.refPxE9)}
      </span>
      <span
        role="cell"
        className="c-status c-full"
        title={
          row.nettingCycleId === ""
            ? "no netting_cycle_id on the notice"
            : row.nettingCycleId
        }
      >
        {l.kind === "transfer" ? (
          "transfer"
        ) : l.kind === "none" ? (
          "no link"
        ) : l.kind === "no-residual" ? (
          "no residual"
        ) : (
          <>
            residual <WireText value={l.clOrdId} />
          </>
        )}
      </span>
      <span role="cell" className="r">
        {MISSING}
      </span>
      <span role="cell" className="r">
        {MISSING}
      </span>
      <span role="cell" className="c-detail">
        px policy <WireText value={row.pxPolicyId || MISSING} />
      </span>
    </>
  );
}

/** Event-driven subject status: "none since connect" until the first event, then "last event Ns ago". Never stale, never a waiting clock. */
function EventStatus({ id, now }: { id: "exec" | "crosses"; now: number }) {
  const conn = useBus((s) => s.conn);
  const last = useBus((s) => s.subjects[id].last);
  const f = freshness(SUBJECTS[id], last, conn, now);
  const age = f.ageMs === null ? null : formatAge(f.ageMs);
  return f.state === "ok" ? (
    <>{age === null ? null : `last event ${age} ago`}</>
  ) : f.state === "waiting" ? (
    <NoneSince />
  ) : (
    <StateChip
      state={f.state}
      clock={f.state === "disconnected" ? null : age}
    />
  );
}

/** [header, class, tooltip]. Qty and Filled hold different things per row kind, so each header names both. */
const HEADERS: [string, string, string?][] = [
  ["", "c-tog"],
  ["Time (UTC)", ""],
  ["Kind", ""],
  ["ClOrdID / CrossId", ""],
  ["Book", ""],
  ["Instr.", ""],
  ["Side", ""],
  [
    "Qty / Fill qty",
    "r",
    "ORDER and CROSS: quantity. ALLOC: Fill qty, this book's share of the fill (last_qty_e2).",
  ],
  ["Px", "r"],
  ["Status / link", ""],
  [
    "Filled / Book filled",
    "r",
    "ORDER: cumulative filled. ALLOC: Book filled, cumulative to this book (book_cum_qty_e2).",
  ],
  ["Leaves", "r"],
  ["Detail", ""],
];

/** Order and execution blotter (ui/CLAUDE.md screen 2): virtualized, newest first, pause-scroll. */
export function BlotterBoard({ now }: { now: number }) {
  const conn = useBus((s) => s.conn);
  const execSt = useBus((s) => s.subjects.exec);
  const crossSt = useBus((s) => s.subjects.crosses);
  const execRing = execSt.rings[""];
  const crossRing = crossSt.rings[""];
  const model = useMemo(
    () => buildBlotter(execRing, crossRing),
    [execRing, crossRing],
  );
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(
    () => new Set(),
  );
  const [filter, setFilter] = useState<BlotterFilter>(NO_FILTER);
  const flat = useMemo(
    () => flattenFiltered(model.items, collapsed, filter),
    [model.items, collapsed, filter],
  );
  const options = useMemo(
    () => filterOptions(model.items, filter),
    [model.items, filter],
  );
  const indexOf = useMemo(
    () => new Map(flat.map((r, i) => [r.key, i])),
    [flat],
  );

  const [rowH] = useState(readRowH);
  const scroller = useRef<HTMLDivElement>(null);
  /** The row at the top of the viewport and how far the viewport sits below its top edge. */
  const anchor = useRef<{ key: string; delta: number } | null>(null);
  /** Set while the user is scrolled away from the top; holds the newest shown row at the time. */
  const [pausedAt, setPausedAt] = useState<string | null>(null);

  const virt = useVirtualizer({
    count: flat.length,
    getScrollElement: () => scroller.current,
    estimateSize: () => rowH,
    overscan: OVERSCAN,
    getItemKey: (i) => flat[i]?.key ?? i,
    initialRect: { width: 0, height: rowH * INITIAL_ROWS },
  });

  /** The scroll position is the user's: remember the top row (so arrivals are pinned) and hold the tail while away from 0. */
  const hold = (el: HTMLElement) => {
    const top = el.scrollTop;
    const idx = Math.floor(top / rowH);
    const row = flat[idx];
    anchor.current = row ? { key: row.key, delta: top - idx * rowH } : null;
    setPausedAt((p) => (top > 0 ? (p ?? flat[0]?.key ?? null) : null));
  };
  const onScroll = (e: UIEvent<HTMLDivElement>) => hold(e.currentTarget);

  // Selection is a row key, never an index: arrivals shift indexes, not keys. A key that leaves the list clears.
  const [selected, setSelected] = useState<string | null>(null);
  const selIdx = selected === null ? undefined : indexOf.get(selected);
  if (selected !== null && selIdx === undefined) setSelected(null);
  const uid = useId();
  const rowId = (i: number) => `${uid}-r${i}`;

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const el = e.currentTarget;
    if (e.target !== el || e.altKey || e.ctrlKey || e.metaKey) return; // keys inside a row's own button stay native
    const last = flat.length - 1;
    let next: number;
    if (e.key === "ArrowDown") next = Math.min((selIdx ?? -1) + 1, last);
    else if (e.key === "ArrowUp") next = Math.max((selIdx ?? 1) - 1, 0);
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = last;
    else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      const row = selIdx === undefined ? undefined : flat[selIdx];
      if (row?.kind === "order" && row.allocs.length > 0) toggle(row.key);
      return;
    } else return;
    e.preventDefault();
    const row = flat[next];
    if (!row) return;
    setSelected(row.key);
    // Bring the row into the viewport (it may be outside the virtualized window), then count it as scrolling.
    const top = next * rowH;
    if (top < el.scrollTop) el.scrollTop = top;
    else if (top + rowH > el.scrollTop + el.clientHeight)
      el.scrollTop = top + rowH - el.clientHeight;
    hold(el);
  };

  // Pause-scroll: rows arrive at the top, so while scrolled away the anchored row is pinned to the same
  // viewport offset. Data stays live (statuses keep updating); only the viewport is held.
  useLayoutEffect(() => {
    const el = scroller.current;
    const a = anchor.current;
    if (!el || !a || el.scrollTop <= 0) return;
    const idx = indexOf.get(a.key);
    if (idx === undefined) return; // the anchored row left the ring window
    const want = idx * rowH + a.delta;
    if (Math.abs(el.scrollTop - want) >= 1) el.scrollTop = want;
  }, [indexOf]);

  const toggle = useCallback(
    (key: string) =>
      setCollapsed((c) => {
        const n = new Set(c);
        if (!n.delete(key)) n.add(key);
        return n;
      }),
    [],
  );

  // A new filter is a new list: go back to the top, live, with nothing held (the anchored row may be gone).
  const refilter = (patch: Partial<BlotterFilter>) => {
    const el = scroller.current;
    if (el) el.scrollTop = 0;
    anchor.current = null;
    setPausedAt(null);
    setFilter((f) => ({ ...f, ...patch }));
  };

  const resume = () => {
    const el = scroller.current;
    if (el) {
      el.scrollTop = 0;
      el.focus();
    }
    anchor.current = null;
    setPausedAt(null);
  };

  const fresh = freshness(SUBJECTS.exec, execSt.last, conn, now);
  const fromConnect =
    conn.firstConnectedAtMs === null
      ? MISSING
      : formatUtcTime(conn.firstConnectedAtMs);
  const newCount =
    pausedAt === null ? 0 : (indexOf.get(pausedAt) ?? flat.length);

  return (
    <section className="blotter" aria-labelledby="p-blotter">
      <section className="panel">
        <PanelHead
          id="p-blotter"
          title="Order and execution blotter"
          hint="d1.exec.<book>.<instrument> and d1.crosses: event streams with no snapshot"
          fresh={fresh}
          ageLabel="last event"
          event
        />
        <div className="bl-bar">
          <div className="bl-filters" role="group" aria-label="Blotter filters">
            <label>
              Kind
              <select
                value={filter.kind}
                onChange={(e) =>
                  refilter({ kind: e.target.value as BlotterFilter["kind"] })
                }
              >
                <option value="">all</option>
                {KINDS.map((k) => (
                  <option key={k} value={k}>
                    {k}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Book
              <select
                value={filter.book}
                onChange={(e) => refilter({ book: e.target.value })}
              >
                <option value="">all</option>
                {options.books.map((b) => (
                  <option key={b} value={String(b)}>
                    {b === 0 ? "0 firm" : b}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Instrument
              <select
                value={filter.instrument}
                onChange={(e) => refilter({ instrument: e.target.value })}
              >
                <option value="">all</option>
                {options.instruments.map((i) => (
                  <option key={i} value={i}>
                    {i}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Status
              <select
                value={filter.status}
                onChange={(e) => refilter({ status: e.target.value })}
              >
                <option value="">all</option>
                {options.statuses.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            </label>
            <label>
              ClOrdID
              <input
                type="search"
                value={filter.text}
                placeholder="ClOrdID / CrossId"
                autoComplete="off"
                spellCheck={false}
                onChange={(e) => refilter({ text: e.target.value })}
              />
            </label>
            {isFiltered(filter) ? (
              <>
                <button
                  type="button"
                  className="bl-clear"
                  onClick={() => refilter(NO_FILTER)}
                >
                  Clear
                </button>
                <span className="bl-count num" aria-live="polite">
                  {flat.length} row{flat.length === 1 ? "" : "s"} match
                </span>
              </>
            ) : null}
          </div>
          <p className="bl-meta num">
            <span
              title={`d1.exec.<book>.<instrument>. Retained window: ${SUBJECTS.exec.ringSize} reports, ${SUBJECTS.crosses.ringSize} crosses.`}
            >
              {execSt.count} execution event{execSt.count === 1 ? "" : "s"}{" "}
              since connect {fromConnect}
            </span>
            <span title="d1.crosses">
              {crossSt.count} cross{crossSt.count === 1 ? "" : "es"} ·{" "}
              <EventStatus id="crosses" now={now} />
            </span>
            <span title="Reports repeating an (exec_id, book_id) already shown, and crosses repeating a cross_id, within the retained window">
              {model.duplicates} duplicate{model.duplicates === 1 ? "" : "s"}{" "}
              ignored
            </span>
          </p>
        </div>
        {flat.length === 0 ? (
          <NoneYetBody>
            {model.items.length === 0
              ? "Exec messages are a stream with no snapshot: an empty blotter means none seen yet, not no orders."
              : "No row matches the filters."}
          </NoneYetBody>
        ) : (
          <XScroll label="Blotter table">
            <div
              className="bl-table"
              role="table"
              aria-label="Orders, allocations and crosses"
              aria-rowcount={flat.length + 1}
            >
              <div className="bl-head" role="rowgroup">
                <div className="bl-row bl-cols" role="row" aria-rowindex={1}>
                  {HEADERS.map(([h, cls, tip], i) => (
                    <span
                      key={i}
                      role="columnheader"
                      className={cls}
                      title={tip}
                      aria-label={h === "" ? "Expand" : undefined}
                    >
                      {h}
                    </span>
                  ))}
                </div>
              </div>
              <div
                className="bl-scroll"
                role="rowgroup"
                ref={scroller}
                tabIndex={0}
                aria-label="Blotter rows, newest first"
                onScroll={onScroll}
                onKeyDown={onKeyDown}
                aria-activedescendant={
                  selIdx === undefined ? undefined : rowId(selIdx)
                }
              >
                <div
                  className="bl-body"
                  style={{ height: `${virt.getTotalSize()}px` }}
                >
                  {virt.getVirtualItems().map((v) => {
                    const row = flat[v.index];
                    if (!row) return null;
                    return (
                      <div
                        key={v.key}
                        id={rowId(v.index)}
                        role="row"
                        aria-rowindex={v.index + 2}
                        aria-selected={row.key === selected}
                        className={
                          row.key === selected
                            ? "bl-row bl-cols is-selected"
                            : "bl-row bl-cols"
                        }
                        data-kind={row.kind}
                        data-key={row.key}
                        style={{ transform: `translateY(${v.start}px)` }}
                      >
                        <Cells
                          row={row}
                          collapsed={collapsed.has(row.key)}
                          toggle={toggle}
                        />
                      </div>
                    );
                  })}
                </div>
              </div>
              {pausedAt !== null && newCount > 0 ? (
                <button type="button" className="bl-resume" onClick={resume}>
                  {newCount} new, resume
                </button>
              ) : null}
            </div>
          </XScroll>
        )}
        <p
          className="bl-foot"
          title="Allocation rows show the book's own book_cum_qty_e2, never the parent's cumulative (ADR-014 Amendment 1 s3). A cross links to its residual order by netting_cycle_id and instrument id (Amendment 1 s4). Reports dedupe on (exec_id, book_id) (ADR-014 s5)."
        >
          Newest first, by first arrival. ALLOC rows sit under their ORDER: Fill
          qty is the book&apos;s share, Book filled its own cumulative. Time is
          UTC, browser arrival.
        </p>
      </section>
    </section>
  );
}
