import { describe, expect, test } from "vitest";
import { formatQtyE2 } from "../format";
import { parseD1, parseExo } from "../health/model";
import {
  HealthStatus,
  ReconAlert,
  RiskSnapshot_LimitLevel as L,
  PositionSnapshot,
  TargetPosition,
} from "../gen/live";
import type { Entry } from "../store";
import {
  bookViews,
  gapQtyE2,
  headroom,
  insideBand,
  joinTargetsPositions,
  limitChipState,
} from "./model";

const WARN = 50_000n;
const HARD = 100_000n;
const bar = (v: bigint, w = WARN, h = HARD) => {
  const r = headroom(v, w, h);
  if (r.kind !== "bar") throw new Error("expected a bar");
  return r;
};

describe("headroom", () => {
  test("zero exposure: empty fill, warn tick still drawn", () => {
    expect(bar(0n)).toEqual({
      kind: "bar",
      fillBps: 0,
      warnBps: 5000,
      over: false,
    });
  });
  test("at warn: fill reaches the tick", () => {
    const r = bar(WARN);
    expect(r.fillBps).toBe(5000);
    expect(r.fillBps).toBe(r.warnBps);
  });
  test("at hard: full, not yet beyond", () => {
    expect(bar(HARD)).toMatchObject({ fillBps: 10_000, over: false });
  });
  test("above hard: clamped to full, beyond-hard mark set", () => {
    expect(bar(HARD * 7n)).toMatchObject({ fillBps: 10_000, over: true });
  });
  test("negative uses the magnitude (limits compare unsigned exposure)", () => {
    expect(bar(-WARN)).toEqual(bar(WARN));
    expect(bar(-HARD * 2n)).toMatchObject({ fillBps: 10_000, over: true });
  });
  test("zero or negative hard cap: limit n/a, never a bar", () => {
    expect(headroom(5n, 0n, 0n)).toEqual({ kind: "na" });
    expect(headroom(5n, 1n, -3n)).toEqual({ kind: "na" });
  });
  test("missing warn cap: no tick; warn above hard: no tick", () => {
    expect(bar(10n, 0n, HARD).warnBps).toBeNull();
    expect(bar(10n, HARD + 1n, HARD).warnBps).toBeNull();
  });
  test("i64 extremes do not lose precision", () => {
    const max = 2n ** 63n - 1n;
    expect(bar(-(2n ** 63n), max, max)).toMatchObject({
      fillBps: 10_000,
      over: true,
    });
    expect(bar(max / 2n, max / 4n, max).fillBps).toBe(4999);
  });
});

describe("limit chip mapping", () => {
  test.each([
    [L.LIMIT_LEVEL_OK, "ok"],
    [L.LIMIT_LEVEL_WARN, "warn"],
    [L.LIMIT_LEVEL_HARD, "hard"],
    [L.LIMIT_LEVEL_UNSPECIFIED, "waiting"],
    [L.UNRECOGNIZED, "waiting"],
  ])("%s -> %s", (level, state) => {
    expect(limitChipState(level)).toBe(state);
  });
});

describe("gap = target - position", () => {
  const MAX = 2n ** 63n - 1n;
  const MIN = -(2n ** 63n);
  test("plain and negative", () => {
    expect(gapQtyE2(800_000n, 710_000n)).toBe(90_000n);
    expect(gapQtyE2(-500n, 300n)).toBe(-800n);
    expect(gapQtyE2(300n, 300n)).toBe(0n);
  });
  test("i64 extremes stay exact (the gap may exceed i64)", () => {
    const g = gapQtyE2(MAX, MIN);
    expect(g).toBe(2n ** 64n - 1n);
    expect(formatQtyE2(g ?? 0n)).toBe("184,467,440,737,095,516.15");
    expect(gapQtyE2(MIN, MAX)).toBe(-(2n ** 64n - 1n));
  });
  test("no target or no position: null, never zero", () => {
    expect(gapQtyE2(null, 5n)).toBeNull();
    expect(gapQtyE2(5n, null)).toBeNull();
  });
  test("band compares the magnitude; unknown band or gap is unknown", () => {
    expect(insideBand(-400n, 500n)).toBe(true);
    expect(insideBand(501n, 500n)).toBe(false);
    expect(insideBand(null, 500n)).toBeNull();
    expect(insideBand(1n, null)).toBeNull();
  });
});

describe("join", () => {
  const at = (v: Entry["value"]): Entry => ({ value: v, receivedAtMs: 1000 });
  const ins = (id: number, symbol: string) => ({
    instrumentId: id,
    symbol,
    instrumentClass: 1,
    currency: "USD",
  });
  test("target absent -> null target (rendered as 'no target yet'); position absent -> null position", () => {
    const rows = joinTargetsPositions(
      {
        "3.1001": at(
          TargetPosition.fromPartial({
            bookId: 3,
            instrument: ins(1001, "AAPL"),
            targetQtyE2: 800_000n,
            bandQtyE2: 50_000n,
          }),
        ),
      },
      {
        "1": at(
          PositionSnapshot.fromPartial({
            bookId: 1,
            lines: [
              {
                instrument: ins(1001, "AAPL"),
                qtyE2: 430_000n,
                inflightQtyE2: 0n,
              },
            ],
          }),
        ),
      },
    );
    expect(rows).toHaveLength(2);
    const [b1, b3] = rows;
    expect(b1).toMatchObject({
      bookId: 1,
      target: null,
      position: 430_000n,
      band: null,
    });
    expect(b3).toMatchObject({
      bookId: 3,
      target: 800_000n,
      position: null,
      band: 50_000n,
    });
  });
  test("target and position on the same (book, instrument) join into one row", () => {
    const rows = joinTargetsPositions(
      {
        k: at(
          TargetPosition.fromPartial({
            bookId: 3,
            instrument: ins(7, "X"),
            targetQtyE2: -9n,
          }),
        ),
      },
      {
        k: at(
          PositionSnapshot.fromPartial({
            bookId: 3,
            lines: [{ instrument: ins(7, "X"), qtyE2: 4n, inflightQtyE2: 2n }],
          }),
        ),
      },
    );
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ target: -9n, position: 4n, inflight: 2n });
  });
});

describe("bookViews: book list and halt state come from the wire", () => {
  const exo = (detail: Record<string, string>) =>
    parseExo(HealthStatus.fromPartial({ component: "exo", ok: true, detail }));
  const d1 = (detail: Record<string, string>) =>
    parseD1(
      HealthStatus.fromPartial({ component: "delta-one", ok: true, detail }),
    );
  const alert = (bookId: number, halted: boolean, at = 0): Entry => ({
    value: ReconAlert.fromPartial({ bookId, publishingHalted: halted }),
    receivedAtMs: at,
  });

  test("the union of exo.health, d1.health and recon alert books, sorted, no valuation needed", () => {
    const v = bookViews(
      [],
      exo({ "book.9.state": "publishing", "book.3.state": "awaiting_marks" }),
      d1({ "limits.book.5.rejects": "0", "limits.book.9.valuation": "ok" }),
      [alert(12, false)],
    );
    expect(v.map((b) => b.bookId)).toEqual([3, 5, 9, 12]);
    expect(v.every((b) => b.rows.length === 0)).toBe(true);
    expect(v.find((b) => b.bookId === 3)?.exoState).toBe("awaiting_marks");
    expect(v.find((b) => b.bookId === 5)?.exoState).toBeUndefined();
  });

  test("halt reasons: halted:<reason> from exo.health, recon from an alert, none otherwise", () => {
    const v = bookViews(
      [],
      exo({
        "book.1.state": "halted:recon",
        "book.2.state": "halted:no-snapshot",
        "book.3.state": "publishing",
        "book.4.state": "awaiting_snapshot",
      }),
      null,
      [alert(7, true)],
    );
    const reasons = (id: number) => v.find((b) => b.bookId === id)?.haltReasons;
    expect(reasons(1)).toEqual(["recon"]);
    expect(reasons(2)).toEqual(["no-snapshot"]);
    expect(reasons(3)).toEqual([]);
    expect(reasons(4)).toEqual([]);
    expect(reasons(7)).toEqual(["recon"]); // alert only, exo.health silent about the book
  });

  test("the latest alert per book decides; a live health state outvotes an alert", () => {
    const ring = [alert(1, true, 1), alert(1, false, 2), alert(2, true, 3)];
    const v = bookViews([], null, null, ring);
    expect(v.find((b) => b.bookId === 1)?.haltReasons).toEqual([]);
    expect(v.find((b) => b.bookId === 2)?.haltReasons).toEqual(["recon"]);
    const w = bookViews([], exo({ "book.2.state": "publishing" }), null, ring);
    expect(w.find((b) => b.bookId === 2)?.haltReasons).toEqual([]);
  });

  test("an unknown halted-looking token is not a halt (the wire carries two values)", () => {
    const v = bookViews(
      [],
      exo({ "book.1.state": "halted:<b>x</b>" }),
      null,
      [],
    );
    expect(v[0]?.haltReasons).toEqual([]);
  });
});
