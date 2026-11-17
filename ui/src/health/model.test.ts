import { describe, expect, test } from "vitest";
import { HealthStatus } from "../gen/live";
import {
  checkGitSha,
  cueFor,
  okCue,
  parseD1,
  parseExo,
  parseU64,
  U64_MAX,
  type TokenKind,
} from "./model";

const hs = (detail: Record<string, string>, ok = true) =>
  HealthStatus.fromPartial({ component: "x", ok, detail });

describe("ok boolean", () => {
  test("true is OK, false is FAULT (HARD cue)", () => {
    expect(okCue(true)).toEqual({ tone: "ok", label: "OK" });
    expect(okCue(false)).toEqual({ tone: "hard", label: "FAULT" });
  });
});

describe("token cues", () => {
  const cases: [TokenKind, string, string, string][] = [
    ["nats", "connected", "ok", "OK"],
    ["nats", "pending", "warn", "PENDING"],
    ["nats", "disconnected", "hard", "FAULT"],
    ["fix", "logged_on", "ok", "OK"],
    ["fix", "logged_out", "hard", "FAULT"],
    ["posttrade", "disabled", "neutral", "DISABLED"],
    ["posttrade", "starting", "warn", "STARTING"],
    ["posttrade", "running", "ok", "OK"],
    ["posttrade", "failed", "hard", "FAULT"],
    ["valuation", "none", "neutral", "NONE"],
    ["valuation", "ok", "ok", "OK"],
    ["valuation", "stale", "warn", "STALE"],
    ["valuation", "invalid", "warn", "INVALID"],
    ["exoBook", "publishing", "ok", "OK"],
    ["exoBook", "halted:recon", "halted", "HALTED"],
    ["exoBook", "halted:no-snapshot", "halted", "HALTED"],
    ["publish", "enabled", "ok", "OK"],
    ["publish", "refused: dirty tree (x)", "hard", "FAULT"],
    ["limit", "warn", "warn", "WARN"],
    ["limit", "hard", "hard", "HARD"],
  ];
  test.each(cases)("%s=%s", (kind, raw, tone, label) => {
    expect(cueFor(kind, raw)).toEqual({ tone, label });
  });

  test.each(["nats", "fix", "posttrade", "valuation", "exoBook"] as const)(
    "%s: an unknown value is UNRECOGNISED, never OK",
    (kind) => {
      for (const raw of ["bogus", "", "OK", "constructor", "__proto__"])
        expect(cueFor(kind, raw)).toEqual({
          tone: "warn",
          label: "UNRECOGNISED",
        });
    },
  );

  test("an absent key is NOT PUBLISHED, not a state", () => {
    expect(cueFor("nats", undefined).label).toBe("NOT PUBLISHED");
  });

  test("a halted: prefix is only a halt when the reason is a plain token", () => {
    expect(cueFor("exoBook", "halted:<img onerror=x>").label).toBe(
      "UNRECOGNISED",
    );
  });
});

describe("u64", () => {
  test("valid decimal, zero, above 2^53 and the u64 maximum", () => {
    expect(parseU64("42")).toEqual({ ok: true, value: 42n });
    expect(parseU64("0")).toEqual({ ok: true, value: 0n });
    expect(parseU64("9007199254740993")).toEqual({
      ok: true,
      value: 9007199254740993n, // 2^53 + 1: Number() would have rounded it
    });
    expect(parseU64(U64_MAX.toString())).toEqual({ ok: true, value: U64_MAX });
  });
  test("non-numeric, signed, fractional, empty and overflowing values are errors, not zero", () => {
    for (const raw of [
      "abc",
      "-1",
      "1.5",
      "",
      " 1",
      "1e3",
      "18446744073709551616",
    ])
      expect(parseU64(raw)).toEqual({ ok: false, raw });
  });
});

describe("parseD1", () => {
  const detail = {
    nats: "connected",
    "fix.session": "logged_on",
    posttrade: "running",
    "posttrade.send_failures": "3",
    "limits.unattributable_rejects": "0",
    "limits.book.10.rejects": "7",
    "limits.book.10.valuation": "ok",
    "limits.book.2.valuation": "none",
    "limits.book.2.rejects": "0",
    "limits.book.x.rejects": "1",
    surprise: "<img src=x onerror=alert(1)>",
  };
  test("splits the contract keys, sorts books numerically, keeps the rest verbatim", () => {
    const d = parseD1(hs(detail));
    expect(d.nats).toBe("connected");
    expect(d.fix).toBe("logged_on");
    expect(d.posttrade).toBe("running");
    expect(d.sendFailures).toBe("3");
    expect(d.unattributable).toBe("0");
    expect(d.books.map((b) => b.id)).toEqual(["2", "10"]);
    expect(d.books[1]).toEqual({ id: "10", rejects: "7", valuation: "ok" });
    expect(d.other).toEqual([
      ["limits.book.x.rejects", "1"],
      ["surprise", "<img src=x onerror=alert(1)>"],
    ]);
  });
});

describe("parseExo", () => {
  test("book, limit and scalar keys", () => {
    const e = parseExo(
      hs({
        git_sha: "abc1234",
        publish: "enabled",
        valuation_in_flight: "false",
        nats: "connected",
        rejected_total: "2",
        "book.3.state": "halted:recon",
        "book.3.last_valuation_ns": "1700000000000000000",
        "limit.7.vega": "hard",
        "limit.7.theta": "warn",
      }),
    );
    expect(e.gitSha).toBe("abc1234");
    expect(e.books).toEqual([
      {
        id: "3",
        state: "halted:recon",
        lastValuationNs: "1700000000000000000",
        lastError: undefined,
      },
    ]);
    expect(e.limits).toEqual([
      { instrumentId: "7", greek: "vega", level: "hard" },
    ]);
    expect(e.other).toEqual([["limit.7.theta", "warn"]]);
  });
});

describe("git_sha provenance check", () => {
  test("mismatch when exo.health and a book differ, -dirty included", () => {
    const c = checkGitSha("4f2a9c1", [
      { bookId: 3, sha: "4f2a9c1" },
      { bookId: 4, sha: "4f2a9c1-dirty" },
    ]);
    expect(c.status).toBe("mismatch");
  });
  test("mismatch between two books with no exo.health", () => {
    expect(
      checkGitSha(undefined, [
        { bookId: 1, sha: "a" },
        { bookId: 2, sha: "b" },
      ]).status,
    ).toBe("mismatch");
  });
  test("consistent when every comparable value is equal", () => {
    expect(checkGitSha("a", [{ bookId: 1, sha: "a" }])).toEqual({
      status: "consistent",
      sha: "a",
      count: 2,
    });
  });
  test("not comparable: a single value, an empty sha, or exo.health's `unavailable`", () => {
    expect(checkGitSha("a", []).status).toBe("insufficient");
    expect(checkGitSha(undefined, [{ bookId: 1, sha: "a" }]).status).toBe(
      "insufficient",
    );
    expect(checkGitSha("a", [{ bookId: 1, sha: "" }]).status).toBe(
      "insufficient",
    );
    expect(checkGitSha("unavailable", [{ bookId: 1, sha: "a" }]).status).toBe(
      "insufficient",
    );
  });
});
