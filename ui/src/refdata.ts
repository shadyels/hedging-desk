/**
 * Instrument and benchmark names from protocol/refdata/universe.json, the one place they are defined
 * (protocol/CLAUDE.md). D1 and EXO put only `instrument_id` on InstrumentRef, so the wire's symbol
 * field is never set: every screen resolves the id here. Bundled at build time (vite inlines the JSON).
 */
import universe from "../../protocol/refdata/universe.json";

const SYMBOLS = new Map<number, string>(
  universe.instruments.map((i) => [i.instrument_id, i.symbol]),
);

/** The symbol for an id, or an explicit "not in reference data" label: never a dash, never empty. */
export const instrumentLabel = (id: bigint | number): string =>
  SYMBOLS.get(Number(id)) ?? `id ${id} (not in reference data)`;

/** A book's benchmark: the book's `benchmark_symbol` and the instrument carrying that symbol. */
export function benchmarkFor(
  bookId: number,
): { symbol: string; instrumentId: number | undefined } | undefined {
  const symbol = universe.books.find(
    (b) => b.book_id === bookId,
  )?.benchmark_symbol;
  if (symbol === undefined) return undefined;
  return {
    symbol,
    instrumentId: universe.instruments.find((i) => i.symbol === symbol)
      ?.instrument_id,
  };
}
