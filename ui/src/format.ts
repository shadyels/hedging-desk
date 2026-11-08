/**
 * The ONE place money and quantities become text (ui/CLAUDE.md rule 2, DESIGN.md "Numbers").
 *
 * Everything here is bigint arithmetic. There is no Number(), parseFloat or float division on a
 * wire value anywhere in this file: i64 extremes (±9.22e18) exceed 2^53 and would silently lose
 * digits. The only `number` inputs are `decimals` (a column setting) and `formatAge` (a clock delta).
 *
 * Conventions (DESIGN.md): thousands separator ",", negative sign U+2212, missing value U+2013.
 * Rounding is half away from zero. A value that rounds to zero prints as "0.00", never "−0.00":
 * a signed zero would tell the trader a position is short when it is flat.
 */

export const MISSING = "–";
const MINUS = "−";

const pow10 = (n: number): bigint => 10n ** BigInt(n);

function assertDigits(name: string, n: number): void {
  if (!Number.isInteger(n) || n < 0)
    throw new RangeError(`${name} must be a non-negative integer, got ${n}`);
}

function group(digits: string): string {
  return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/** Format `v / 10^scale` with exactly `decimals` fraction digits. Pure, bigint-only. */
export function formatFixed(
  v: bigint,
  scale: number,
  decimals: number,
): string {
  assertDigits("scale", scale);
  assertDigits("decimals", decimals);
  const negative = v < 0n;
  const mag = negative ? -v : v; // BigInt: -(-2^63) is fine
  let q: bigint; // |v| rescaled to `decimals` fraction digits, rounded half away from zero
  if (decimals >= scale) {
    q = mag * pow10(decimals - scale);
  } else {
    const div = pow10(scale - decimals);
    q = mag / div;
    if ((mag % div) * 2n >= div) q += 1n;
  }
  const unit = pow10(decimals);
  const int = group((q / unit).toString());
  const frac =
    decimals === 0 ? "" : "." + (q % unit).toString().padStart(decimals, "0");
  return (negative && q !== 0n ? MINUS : "") + int + frac;
}

/** qty_e2 (quantity x 10^2). Default 2 decimals is the native precision. */
export const formatQtyE2 = (v: bigint, decimals = 2): string =>
  formatFixed(v, 2, decimals);

/** price_e9 and every other plain `_e9` value (notional, vega, rho...). Decimals are per column. */
export const formatE9 = (v: bigint, decimals: number): string =>
  formatFixed(v, 9, decimals);
export const formatPxE9 = formatE9;

/** `_e9` decimal fraction as basis points: 0.0035 (= 3_500_000n) -> "35.0". bps = fraction x 10^4. */
export const formatBpsE9 = (v: bigint, decimals = 1): string =>
  formatFixed(v, 5, decimals);

/** `_e9` decimal fraction as percent: 0.0035 (= 3_500_000n) -> "0.35". percent = fraction x 10^2. */
export const formatPctE9 = (v: bigint, decimals = 2): string =>
  formatFixed(v, 7, decimals);

/** Elapsed milliseconds as a chip clock: "47s", "1m05s", "1h02m". A clock delta, not money. */
export function formatAge(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}m`;
}

/** Epoch ms as "12:04:09Z" (UTC, DESIGN.md). A timestamp, not money. */
export const formatUtcTime = (ms: number): string =>
  new Date(ms).toISOString().slice(11, 19) + "Z";
