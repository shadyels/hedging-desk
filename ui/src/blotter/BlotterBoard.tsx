import { useVirtualizer } from "@tanstack/react-virtual";
import {
  useCallback,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type UIEvent,
} from "react";
import { freshness } from "../bus/freshness";
import { SUBJECTS } from "../bus/subjects";
import { StateChip } from "../components/StateChip";
import { WireText } from "../components/WireText";
import {
  MISSING,
  formatAge,
  formatPxE9,
  formatQtyE2,
  formatUtcTime,
} from "../format";
import type { InstrumentRef } from "../gen/common";
import { PanelHead, WaitingBody } from "../risk/parts";
import { useBus } from "../store";
import {
  buildBlotter,
  flatten,
  sideLabel,
  statusGlyph,
  statusLabel,
  type AllocRow,
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
  return <WireText value={i.symbol || String(i.instrumentId)} />;
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
      <span role="cell">alloc</span>
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
      <span role="cell" className="c-id">
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
        className="c-status"
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

/** Event-driven subject status: waiting chip until the first event, then "last event Ns ago". Never stale. */
function EventStatus({ id, now }: { id: "exec" | "crosses"; now: number }) {
  const conn = useBus((s) => s.conn);
  const last = useBus((s) => s.subjects[id].last);
  const f = freshness(SUBJECTS[id], last, conn, now);
  const age = f.ageMs === null ? null : formatAge(f.ageMs);
  return f.state === "ok" ? (
    <>{age === null ? null : `last event ${age} ago`}</>
  ) : (
    <StateChip
      state={f.state}
      clock={f.state === "disconnected" ? null : age}
    />
  );
}

const HEADERS: [string, string][] = [
  ["", "c-tog"],
  ["Time (UTC)", ""],
  ["Kind", ""],
  ["ClOrdID / CrossId", ""],
  ["Book", ""],
  ["Instr.", ""],
  ["Side", ""],
  ["Qty", "r"],
  ["Px", "r"],
  ["Status / link", ""],
  ["Filled", "r"],
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
  const flat = useMemo(
    () => flatten(model.items, collapsed),
    [model.items, collapsed],
  );
  const indexOf = useMemo(
    () => new Map(flat.map((r, i) => [r.key, i])),
    [flat],
  );

  const [rowH] = useState(readRowH);
  const scroller = useRef<HTMLDivElement>(null);
  /** The row at the top of the viewport and how far the viewport sits below its top edge. */
  const anchor = useRef<{ key: string; delta: number } | null>(null);
  /** Set while the user is scrolled away from the top; holds the newest top-level row at the time. */
  const [pausedAt, setPausedAt] = useState<string | null>(null);

  const virt = useVirtualizer({
    count: flat.length,
    getScrollElement: () => scroller.current,
    estimateSize: () => rowH,
    overscan: OVERSCAN,
    getItemKey: (i) => flat[i]?.key ?? i,
    initialRect: { width: 0, height: rowH * INITIAL_ROWS },
  });

  const onScroll = (e: UIEvent<HTMLDivElement>) => {
    const top = e.currentTarget.scrollTop;
    const idx = Math.floor(top / rowH);
    const row = flat[idx];
    anchor.current = row ? { key: row.key, delta: top - idx * rowH } : null;
    setPausedAt((p) => (top > 0 ? (p ?? model.items[0]?.key ?? null) : null));
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
  let newCount = 0;
  if (pausedAt !== null) {
    const i = model.items.findIndex((r) => r.key === pausedAt);
    newCount = i < 0 ? model.items.length : i;
  }

  return (
    <section className="blotter" aria-labelledby="p-blotter">
      <section className="panel">
        <PanelHead
          id="p-blotter"
          title="Order and execution blotter"
          note={<code>d1.exec.&lt;book&gt;.&lt;instrument&gt;</code>}
          fresh={fresh}
          ageLabel="last event"
        />
        <p className="bl-meta num">
          <span>
            {execSt.count} execution event{execSt.count === 1 ? "" : "s"} since
            connect {fromConnect}
          </span>
          <span>
            <code>d1.crosses</code> {crossSt.count} ·{" "}
            <EventStatus id="crosses" now={now} />
          </span>
          <span title="Reports repeating an (exec_id, book_id) already shown, and crosses repeating a cross_id, within the retained window">
            {model.duplicates} duplicate{model.duplicates === 1 ? "" : "s"}{" "}
            ignored
          </span>
          <span>
            window {SUBJECTS.exec.ringSize} reports, {SUBJECTS.crosses.ringSize}{" "}
            crosses
          </span>
        </p>
        {flat.length === 0 ? (
          <WaitingBody
            what={
              <>
                <code>d1.exec.*.*</code> or <code>d1.crosses</code>
              </>
            }
            extra="Exec messages are a stream with no snapshot: an empty blotter means none seen yet, not no orders."
          />
        ) : (
          <div
            className="bl-table"
            role="table"
            aria-label="Orders, allocations and crosses"
            aria-rowcount={flat.length + 1}
          >
            <div className="bl-head" role="rowgroup">
              <div className="bl-row bl-cols" role="row" aria-rowindex={1}>
                {HEADERS.map(([h, cls], i) => (
                  <span
                    key={i}
                    role="columnheader"
                    className={cls}
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
                      role="row"
                      aria-rowindex={v.index + 2}
                      className="bl-row bl-cols"
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
        )}
        <p className="bl-foot">
          Newest first, by first arrival. Allocation rows are indented under
          their parent order; their qty is the book&apos;s share and filled is
          its own cumulative, never the parent&apos;s. A cross links to its
          residual order by netting_cycle_id and instrument. Time is browser
          arrival, UTC.
        </p>
      </section>
    </section>
  );
}
