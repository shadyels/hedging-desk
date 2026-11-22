import { instrumentLabel } from "../refdata";

/** An instrument by id, as its reference-data symbol. The id stays in the tooltip so the mapping is auditable. */
export function Instrument({ id }: { id: bigint | number }) {
  return (
    <span className="wire-text" title={`instrument_id ${id}`}>
      {instrumentLabel(id)}
    </span>
  );
}
