import { describe, expect, test } from "vitest";
import {
  MISSING,
  formatAge,
  formatBpsE9,
  formatE9,
  formatFixed,
  formatPctE9,
  formatPxE9,
  formatQtyE2,
  formatUtcTime,
} from "./format";

const I64_MIN = -9223372036854775808n;
const I64_MAX = 9223372036854775807n;

describe("formatQtyE2", () => {
  test("zero prints 0.00", () => {
    expect(formatQtyE2(0n)).toBe("0.00");
  });
  test("groups thousands with a comma", () => {
    expect(formatQtyE2(1230000n)).toBe("12,300.00");
    expect(formatQtyE2(100000000n)).toBe("1,000,000.00");
  });
  test("negative uses U+2212, not a hyphen", () => {
    expect(formatQtyE2(-415000n)).toBe("−4,150.00");
    expect(formatQtyE2(-415000n)).not.toContain("-");
  });
  test("sub-unit fractions keep leading zeros", () => {
    expect(formatQtyE2(1n)).toBe("0.01");
    expect(formatQtyE2(-1n)).toBe("−0.01");
    expect(formatQtyE2(99n)).toBe("0.99");
    expect(formatQtyE2(101n)).toBe("1.01");
  });
  test("i64 extremes lose no digits", () => {
    expect(formatQtyE2(I64_MIN)).toBe("−92,233,720,368,547,758.08");
    expect(formatQtyE2(I64_MAX)).toBe("92,233,720,368,547,758.07");
  });
  test("fewer decimals round half away from zero", () => {
    expect(formatQtyE2(150n, 0)).toBe("2");
    expect(formatQtyE2(149n, 0)).toBe("1");
    expect(formatQtyE2(-150n, 0)).toBe("−2");
    expect(formatQtyE2(-149n, 0)).toBe("−1");
  });
  test("more decimals pad with zeros", () => {
    expect(formatQtyE2(12345n, 4)).toBe("123.4500");
  });
});

describe("formatPxE9 / formatE9", () => {
  test("i64 extremes at several precisions", () => {
    expect(formatPxE9(I64_MAX, 2)).toBe("9,223,372,036.85");
    expect(formatPxE9(I64_MAX, 9)).toBe("9,223,372,036.854775807");
    expect(formatPxE9(I64_MIN, 9)).toBe("−9,223,372,036.854775808");
    expect(formatPxE9(I64_MIN, 0)).toBe("−9,223,372,037");
  });
  test("exact price at column precision", () => {
    expect(formatPxE9(150250000000n, 4)).toBe("150.2500");
    expect(formatE9(1084437000n, 5)).toBe("1.08444");
  });
  test("rounding boundary is half away from zero", () => {
    expect(formatPxE9(4999999n, 2)).toBe("0.00");
    expect(formatPxE9(5000000n, 2)).toBe("0.01");
    expect(formatPxE9(-5000000n, 2)).toBe("−0.01");
    expect(formatPxE9(2500000000n, 0)).toBe("3");
    expect(formatPxE9(-2500000000n, 0)).toBe("−3");
  });
  test("a negative that rounds to zero prints 0.00, never a signed zero", () => {
    expect(formatPxE9(-1n, 2)).toBe("0.00");
    expect(formatPxE9(-4999999n, 2)).toBe("0.00");
    expect(formatPxE9(-1n, 2)).not.toContain("−");
    expect(formatPxE9(0n, 2)).toBe("0.00");
  });
  test("carry across the decimal point", () => {
    expect(formatPxE9(999999999n, 2)).toBe("1.00");
    expect(formatPxE9(999500000n, 2)).toBe("1.00");
    expect(formatPxE9(994999999n, 2)).toBe("0.99");
  });
});

describe("bps and fractions (e9 decimal fractions)", () => {
  test("3_500_000 = 0.0035 = 35 bps", () => {
    expect(formatBpsE9(3500000n)).toBe("35.0");
    expect(formatBpsE9(3500000n, 2)).toBe("35.00");
  });
  test("negative bps", () => {
    expect(formatBpsE9(-1240000n)).toBe("−12.4");
  });
  test("bps rounding is half away from zero at the requested decimals", () => {
    expect(formatBpsE9(3544999n)).toBe("35.4");
    expect(formatBpsE9(3545000n)).toBe("35.5");
    expect(formatBpsE9(-3545000n)).toBe("−35.5");
  });
  test("sub-bp values round to zero without a sign", () => {
    expect(formatBpsE9(49n)).toBe("0.0");
    expect(formatBpsE9(-49n)).toBe("0.0");
    expect(formatBpsE9(0n)).toBe("0.0");
  });
  test("i64 extremes", () => {
    expect(formatBpsE9(I64_MAX)).toBe("92,233,720,368,547.8");
    expect(formatBpsE9(I64_MIN)).toBe("−92,233,720,368,547.8");
  });
  test("percent", () => {
    expect(formatPctE9(3500000n)).toBe("0.35");
    expect(formatPctE9(-3500000n)).toBe("−0.35");
    expect(formatPctE9(1000000000n)).toBe("100.00");
  });
});

describe("formatFixed guards", () => {
  test("rejects a bad scale or decimals instead of guessing", () => {
    expect(() => formatFixed(1n, 2, -1)).toThrow(RangeError);
    expect(() => formatFixed(1n, 2, 1.5)).toThrow(RangeError);
    expect(() => formatFixed(1n, -2, 2)).toThrow(RangeError);
  });
  test("missing is an en dash", () => {
    expect(MISSING).toBe("–");
  });
});

describe("formatAge / formatUtcTime", () => {
  test("seconds, minutes, hours", () => {
    expect(formatAge(0)).toBe("0s");
    expect(formatAge(47_000)).toBe("47s");
    expect(formatAge(65_000)).toBe("1m05s");
    expect(formatAge(192_000)).toBe("3m12s");
    expect(formatAge(3_720_000)).toBe("1h02m");
  });
  test("a negative delta (clock skew) clamps to 0s", () => {
    expect(formatAge(-5_000)).toBe("0s");
  });
  test("UTC wall clock", () => {
    expect(formatUtcTime(Date.UTC(2026, 10, 8, 12, 4, 9))).toBe("12:04:09Z");
  });
});
