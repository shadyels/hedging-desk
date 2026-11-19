import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { SUBJECTS } from "../bus/subjects";
import { HealthStatus, ValuationSnapshot } from "../gen/live";
import { busSink, flushBus, resetBus } from "../store";
import { useNow } from "../useNow";
import { HealthBoard } from "./HealthBoard";

let seq = 0;
const T0 = Date.UTC(2026, 10, 8, 12, 4, 9);
beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(T0);
  resetBus();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function Host() {
  return <HealthBoard now={useNow()} />;
}
const up = () =>
  act(() =>
    busSink.onStatus(
      "connected",
      { attempt: 0, nextRetryAtMs: null },
      Date.now(),
    ),
  );
const tick = (ms: number) => act(() => void vi.advanceTimersByTime(ms));

const d1Detail = {
  nats: "connected",
  "fix.session": "logged_on",
  posttrade: "running",
  "posttrade.send_failures": "9007199254740993",
  "limits.unattributable_rejects": "0",
  "limits.book.1.rejects": "4",
  "limits.book.1.valuation": "ok",
  "limits.book.2.rejects": "0",
  "limits.book.2.valuation": "none",
};
const send = (
  id: "d1Health" | "exoHealth",
  detail: Record<string, string>,
  ok = true,
) =>
  act(() => {
    busSink.onMessage(
      id,
      SUBJECTS[id].pattern,
      "",
      HealthStatus.fromPartial({
        meta: { msgId: `h-${seq++}` },
        component: id === "d1Health" ? "delta-one" : "exo",
        ok,
        detail,
      }),
      Date.now(),
    );
    flushBus();
  });
const sendVal = (book: number, gitSha: string) =>
  act(() => {
    busSink.onMessage(
      "valuations",
      `exo.valuations.${book}`,
      String(book),
      ValuationSnapshot.fromPartial({
        meta: { msgId: `v-${seq++}` },
        bookId: book,
        asOfNs: 1_700_000_000_000_000_000n,
        valuation: {
          modelId: "heston-qe-v1",
          paramsHash: "f".repeat(64),
          seed: 20260n,
          nPaths: 200_000,
          gitSha,
        },
        bookPvE9: 0n,
      }),
      Date.now(),
    );
    flushBus();
  });
const chips = (c: HTMLElement) =>
  [...c.querySelectorAll(".chip")].map((n) => n.textContent ?? "");

describe("health screen", () => {
  test("waiting with no messages: no state, no number, not stale", () => {
    const { container } = render(<Host />);
    up();
    tick(300_000);
    expect(container.textContent).toContain("Waiting for the first");
    expect(
      container.querySelector('.chip[data-state="waiting"]'),
    ).not.toBeNull();
    expect(container.querySelector('.chip[data-state="stale"]')).toBeNull();
    expect(container.querySelector("table")).toBeNull();
  });

  test("renders a fixture d1.health: overall chip, tokens, u64 above 2^53, per-book states", () => {
    const { container } = render(<Host />);
    up();
    send("d1Health", d1Detail);
    const t = container.textContent ?? "";
    expect(chips(container)).toContain("OK"); // the glyph is CSS, so the text is the label only
    expect(t).toContain("9,007,199,254,740,993");
    expect(t).toContain("NONE");
    expect(t).toContain("book 1");
    expect(t).toContain("book 2");
    expect(container.querySelector('[aria-labelledby="p-d1"]')).not.toBeNull();
  });

  test("ok=false shows FAULT", () => {
    const { container } = render(<Host />);
    up();
    send("d1Health", { ...d1Detail, "fix.session": "logged_out" }, false);
    expect(chips(container).filter((c) => c === "FAULT").length).toBe(2);
  });

  test("an unknown token shows UNRECOGNISED and the raw text, never OK", () => {
    const { container } = render(<Host />);
    up();
    send("d1Health", { ...d1Detail, "fix.session": "mystery" });
    expect(chips(container)).toContain("UNRECOGNISED");
    expect(container.textContent).toContain("mystery");
  });

  test("send_failures is hidden when posttrade is disabled, shown otherwise", () => {
    const { container } = render(<Host />);
    up();
    send("d1Health", { ...d1Detail, posttrade: "disabled" });
    expect(container.textContent).not.toContain("posttrade.send_failures");
    send("d1Health", d1Detail);
    expect(container.textContent).toContain("posttrade.send_failures");
  });

  test("a non-numeric u64 is an error shown as text, not a zero", () => {
    const { container } = render(<Host />);
    up();
    send("d1Health", { ...d1Detail, "limits.unattributable_rejects": "n/a" });
    expect(chips(container)).toContain("NOT A u64");
    expect(container.textContent).toContain("n/a");
  });

  test("per-book valuation: each of none, ok, stale, invalid and an unknown value", () => {
    const { container } = render(<Host />);
    up();
    send("d1Health", {
      ...d1Detail,
      "limits.book.3.valuation": "stale",
      "limits.book.4.valuation": "invalid",
      "limits.book.5.valuation": "weird",
    });
    const rows = [
      ...container.querySelectorAll('[aria-labelledby="p-d1-books"] tbody tr'),
    ].map((r) => r.textContent ?? "");
    expect(rows[0]).toContain("OK");
    expect(rows[1]).toContain("NONE");
    expect(rows[2]).toContain("STALE");
    expect(rows[3]).toContain("INVALID");
    expect(rows[4]).toContain("UNRECOGNISED");
    expect(rows[4]).toContain("weird");
  });

  test("waiting vs stale: d1.health stale after 3 s, exo.health still waiting", () => {
    const { container } = render(<Host />);
    up();
    send("d1Health", d1Detail);
    tick(5_000);
    const d1 = container.querySelector('[aria-labelledby="p-d1"]');
    const exo = container.querySelector('[aria-labelledby="p-exo"]');
    expect(d1?.querySelector('.chip[data-state="stale"]')).not.toBeNull();
    expect(exo?.querySelector('.chip[data-state="waiting"]')).not.toBeNull();
    expect(exo?.querySelector('.chip[data-state="stale"]')).toBeNull();
  });

  test("exo.health: git_sha, a HALTED book and limit levels", () => {
    const { container } = render(<Host />);
    up();
    send(
      "exoHealth",
      {
        git_sha: "4f2a9c1",
        publish: "enabled",
        valuation_in_flight: "false",
        nats: "connected",
        rejected_total: "0",
        "book.3.state": "halted:recon",
        "book.3.last_valuation_ns": "1700000000000000000",
        "limit.7.vega": "hard",
      },
      false,
    );
    expect(container.textContent).toContain("4f2a9c1");
    expect(chips(container)).toEqual(
      expect.arrayContaining(["FAULT", "HALTED", "HARD"]),
    );
  });

  test("git_sha mismatch warning: present on differing values, absent when equal or not comparable", () => {
    const { container } = render(<Host />);
    up();
    sendVal(3, "4f2a9c1");
    // one value only: not comparable, no warning
    expect(container.querySelector(".hbanner")).toBeNull();
    expect(container.textContent).toContain("NOT COMPARED");
    send("exoHealth", { git_sha: "4f2a9c1" });
    expect(container.querySelector(".hbanner")).toBeNull();
    expect(container.textContent).toContain("all 2 git_sha values");
    sendVal(4, "4f2a9c1-dirty");
    const b = container.querySelector(".hbanner");
    expect(b).not.toBeNull();
    expect(b?.textContent).toContain("4f2a9c1-dirty");
    expect(b?.textContent).toContain("book 3");
    send("exoHealth", { git_sha: "unavailable" });
    // exo.health sha is not a sha: books still differ from each other
    expect(container.querySelector(".hbanner")).not.toBeNull();
  });

  test("valuation metadata table shows model, seed, n_paths, git_sha, params_hash", () => {
    const { container } = render(<Host />);
    up();
    sendVal(3, "4f2a9c1");
    const t = container.textContent ?? "";
    expect(t).toContain("heston-qe-v1");
    expect(t).toContain("20260");
    expect(t).toContain("200,000");
    expect(t).toContain("f".repeat(64));
  });

  test("XSS: a detail key or value with markup renders as literal text", () => {
    const { container } = render(<Host />);
    up();
    const evil = "<img src=x onerror=alert(1)>";
    send("d1Health", {
      ...d1Detail,
      [evil]: evil,
      "limits.book.9.valuation": evil,
    });
    send("exoHealth", { git_sha: evil, [evil]: evil, "book.1.state": evil });
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain(evil);
  });
});
