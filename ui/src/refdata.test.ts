import { describe, expect, test } from "vitest";
import { benchmarkFor, instrumentLabel } from "./refdata";

describe("reference data resolver", () => {
  test("known ids give their symbol (bigint or number); an unknown id gets the explicit label", () => {
    expect(instrumentLabel(1001)).toBe("AAPL");
    expect(instrumentLabel(1001n)).toBe("AAPL");
    expect(instrumentLabel(2001)).toBe("SPX");
    expect(instrumentLabel(424242)).toBe("id 424242 (not in reference data)");
    expect(instrumentLabel(0)).toBe("id 0 (not in reference data)");
  });

  test("a book's benchmark is its benchmark_symbol and the instrument with that symbol", () => {
    expect(benchmarkFor(1)).toEqual({ symbol: "SPX", instrumentId: 2001 });
    expect(benchmarkFor(2)).toBeUndefined();
    expect(benchmarkFor(999)).toBeUndefined();
  });
});

describe("the UI never reads a symbol off the wire", () => {
  // D1 and EXO leave InstrumentRef.symbol empty, so every printed instrument must go through refdata.ts (which reads
  // the reference file's own symbol, so it is the one exclusion).
  const sources = import.meta.glob(
    ["./**/*.ts", "./**/*.tsx", "!./**/*.test.*", "!./gen/**", "!./refdata.ts"],
    { query: "?raw", import: "default", eager: true },
  ) as Record<string, string>;

  test("no non-test source under src/ reads `.symbol`", () => {
    expect(Object.keys(sources).length).toBeGreaterThan(20);
    const bad = Object.keys(sources).filter((f) =>
      /\.symbol\b/.test(sources[f] ?? ""),
    );
    expect(bad).toEqual([]);
  });
});
