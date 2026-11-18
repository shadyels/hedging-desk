import { useNow } from "../useNow";
import { AlertTicker } from "./AlertTicker";
import { BooksTable } from "./BooksTable";
import { FirmTable } from "./FirmTable";
import { TargetBand } from "./TargetBand";

/** Desk risk board (ui/CLAUDE.md screen 1, minus the tracker scorecard which is its own screen). */
export function RiskBoard() {
  const now = useNow();
  return (
    <div className="risk">
      <FirmTable now={now} />
      <BooksTable now={now} />
      <TargetBand now={now} />
      <AlertTicker now={now} />
    </div>
  );
}
