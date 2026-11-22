import { useMemo } from "react";
import { asHealth, parseD1, parseExo } from "../health/model";
import { useBus } from "../store";
import { bookViews, valuationRows } from "./model";

/** Every book the wire names, with its halt state: shared by the Books table and the shell roll-up. */
export function useBookViews() {
  const st = useBus((s) => s.subjects.valuations);
  const exoLast = useBus((s) => s.subjects.exoHealth.last);
  const d1Last = useBus((s) => s.subjects.d1Health.last);
  const reconSt = useBus((s) => s.subjects.reconAlerts);
  return useMemo(() => {
    const exoMsg = asHealth(exoLast?.value);
    const d1Msg = asHealth(d1Last?.value);
    return bookViews(
      valuationRows(st.latest),
      exoMsg ? parseExo(exoMsg) : null,
      d1Msg ? parseD1(d1Msg) : null,
      reconSt.rings[""] ?? [],
    );
  }, [st.latest, exoLast, d1Last, reconSt.rings]);
}
