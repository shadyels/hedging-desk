import { describe, expect, test } from "vitest";
import md from "../../../protocol/nats-subjects.md?raw";
import * as live from "../gen/live";
import {
  SUBJECTS,
  SUBJECT_IDS,
  keyOf,
  staleThresholdMs,
  type WithMeta,
} from "./subjects";

interface Row {
  pattern: string;
  consumers: string;
  payload: string;
}

/** Parse the subject table of protocol/nats-subjects.md. `<book>` placeholders become `*`. */
function parseTable(src: string): Row[] {
  const rows: Row[] = [];
  for (const line of src.split("\n")) {
    if (!line.startsWith("|")) continue;
    const cells = line
      .split("|")
      .slice(1, -1)
      .map((c) => c.trim());
    const subject = /`([^`]+)`/.exec(cells[0] ?? "")?.[1];
    if (!subject || cells.length < 5 || !subject.includes(".")) continue; // header, separator
    rows.push({
      pattern: subject.replace(/<[^>]+>/g, "*"),
      consumers: cells[2] ?? "",
      payload:
        /([A-Za-z]+)/.exec((cells[3] ?? "").replace(/`/g, ""))?.[1] ?? "",
    });
  }
  return rows;
}

const table = parseTable(md);
const byPattern = new Map(table.map((r) => [r.pattern, r]));

/** UI is a consumer in nats-subjects.md but this slice deliberately does not subscribe. Adding a
 *  subject that the UI consumes without registering it (or listing it here) fails the test below. */
const NOT_SUBSCRIBED: Record<string, string> = {
  "exo.proposals.*": "HedgeProposal: P3.M2 (approve/reject)",
  "sim.md.*": "simulated tape: display only, not in P3.M1 scope",
};

describe("registry matches protocol/nats-subjects.md", () => {
  test("the parser found the table", () => {
    expect(table.length).toBeGreaterThan(10);
    expect(byPattern.has("d1.health")).toBe(true);
  });

  test("every registry subject is a documented row with the same payload and the UI as a consumer", () => {
    for (const id of SUBJECT_IDS) {
      const spec = SUBJECTS[id];
      const row = byPattern.get(spec.pattern);
      expect(
        row,
        `${id}: ${spec.pattern} is not in nats-subjects.md`,
      ).toBeDefined();
      expect(row?.payload, `${id}: payload`).toBe(spec.payload);
      expect(row?.consumers, `${id}: UI must be a consumer`).toMatch(/\bUI\b/);
    }
  });

  test("every subject the UI consumes is registered or explicitly deferred", () => {
    const registered = new Set(SUBJECT_IDS.map((id) => SUBJECTS[id].pattern));
    for (const row of table) {
      if (!/\bUI\b/.test(row.consumers)) continue;
      expect(
        registered.has(row.pattern) || row.pattern in NOT_SUBSCRIBED,
        `${row.pattern} is consumed by the UI but neither registered nor deferred`,
      ).toBe(true);
    }
  });

  test("the divergence check bites: an unknown pattern is not in the table", () => {
    expect(byPattern.has("d1.positions.*.extra")).toBe(false);
    expect(byPattern.has("d1.bogus")).toBe(false);
  });
});

describe("decoders", () => {
  const CLASSES: Record<string, { fromPartial(o: object): WithMeta }> = {
    RiskSnapshot: live.RiskSnapshot,
    RiskLimitAlert: live.RiskLimitAlert,
    PositionSnapshot: live.PositionSnapshot,
    TargetPosition: live.TargetPosition,
    ValuationSnapshot: live.ValuationSnapshot,
    ReconAlert: live.ReconAlert,
    ExecutionReport: live.ExecutionReport,
    InternalCrossNotice: live.InternalCrossNotice,
    TrackerAnalytics: live.TrackerAnalytics,
    HealthStatus: live.HealthStatus,
    MarkPrice: live.MarkPrice,
  };

  test("each decoder is wired to the generated class named in its payload field", () => {
    for (const id of SUBJECT_IDS) {
      const spec = SUBJECTS[id];
      const cls = CLASSES[spec.payload];
      expect(cls, `${id}: no class for ${spec.payload}`).toBeDefined();
      expect(spec.decoder(new Uint8Array())).toEqual(cls?.fromPartial({}));
    }
  });
});

describe("keyOf", () => {
  test("wildcard tokens only", () => {
    expect(keyOf("d1.positions.*", "d1.positions.7")).toBe("7");
    expect(keyOf("exo.targets.*.*", "exo.targets.3.12")).toBe("3.12");
    expect(keyOf("d1.risk.firm", "d1.risk.firm")).toBe("");
  });
});

describe("staleness thresholds", () => {
  const msg = (samplingIntervalS: number): WithMeta =>
    live.TrackerAnalytics.fromPartial({ samplingIntervalS });
  test("1 s cadence subjects: 3 s; valuations: 12 s", () => {
    for (const id of [
      "riskFirm",
      "positions",
      "d1Health",
      "exoHealth",
    ] as const) {
      expect(staleThresholdMs(SUBJECTS[id], msg(0))).toBe(3000);
    }
    expect(staleThresholdMs(SUBJECTS.valuations, msg(0))).toBe(12000);
  });
  test("tracker: 2.5 x the sampling interval carried by the message", () => {
    expect(staleThresholdMs(SUBJECTS.tracker, msg(60))).toBe(150_000);
    expect(staleThresholdMs(SUBJECTS.tracker, msg(1))).toBe(2500);
  });
  test("event-driven and undecided subjects have no time threshold", () => {
    for (const id of [
      "targets",
      "limitAlerts",
      "reconAlerts",
      "crosses",
      "exec",
      "marks",
    ] as const) {
      expect(staleThresholdMs(SUBJECTS[id], msg(0))).toBeNull();
    }
    expect(SUBJECTS.marks.staleAfterMs).toBe("undecided");
    expect(SUBJECTS.exec.staleAfterMs).toBe("never");
  });
});
