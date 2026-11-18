/**
 * Monitoring shell: product name, four screen tabs, trust roll-up, connection, UTC clock and theme. One screen is
 * shown at a time; the tab lives in the URL hash. The screens are in risk/, tracker/, blotter/ and health/.
 */
import { StateChip } from "./components/StateChip";
import { formatAge, formatUtcTime } from "./format";
import { BlotterBoard } from "./blotter/BlotterBoard";
import { HealthBoard } from "./health/HealthBoard";
import { SubjectsTable } from "./health/SubjectsTable";
import { RiskBoard } from "./risk/RiskBoard";
import { SCREENS, useScreen, useTheme, type ScreenId } from "./shell/nav";
import { useRollup } from "./shell/rollup";
import { TabBar } from "./shell/TabBar";
import { useBus } from "./store";
import { TrackerBoard } from "./tracker/TrackerBoard";
import { useNow } from "./useNow";

function Screen({ id, now }: { id: ScreenId; now: number }) {
  switch (id) {
    case "risk":
      return <RiskBoard />;
    case "tracker":
      return <TrackerBoard now={now} />;
    case "blotter":
      return <BlotterBoard now={now} />;
    case "health":
      return (
        <>
          <HealthBoard now={now} />
          <SubjectsTable now={now} />
        </>
      );
  }
}

export function App() {
  const conn = useBus((s) => s.conn);
  const now = useNow();
  const [screen, go] = useScreen();
  const [theme, toggleTheme] = useTheme();
  const trust = useRollup(now);
  const up = conn.status === "connected";
  // First dial: nothing was lost, so this is CONNECTING, not DISCONNECTED, and there is no time to show.
  const connecting = conn.status === "connecting";
  const since =
    conn.disconnectedSinceMs === null
      ? ""
      : ` since ${formatUtcTime(conn.disconnectedSinceMs)}`;
  const lastData =
    conn.lastDataAtMs === null
      ? "no data received"
      : `last data ${formatAge(now - conn.lastDataAtMs)} old`;
  const retry =
    conn.status === "reconnecting" && conn.nextRetryAtMs !== null
      ? `, attempt ${conn.attempt}, retry in ${formatAge(Math.max(0, conn.nextRetryAtMs - now))}`
      : "";
  const label = SCREENS.find((s) => s.id === screen)?.label ?? "";
  return (
    <>
      <header className="bar">
        <h1 className="bar-name">Desk Hedging</h1>
        <TabBar active={screen} onSelect={go} />
        <span
          className="bar-trust"
          title={
            trust.reasons.length > 0
              ? trust.reasons.join("; ")
              : "Every feed is fresh and no limit is above OK"
          }
        >
          Data <StateChip state={trust.state} label={trust.label} />
        </span>
        <span className="bar-conn">
          <StateChip
            state={up ? "ok" : "disconnected"}
            label={up ? "CONNECTED" : conn.status.toUpperCase()}
          />
        </span>
        <time className="bar-clock num" dateTime={new Date(now).toISOString()}>
          {formatUtcTime(now)}
        </time>
        <button
          type="button"
          className="bar-theme"
          onClick={toggleTheme}
          title={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
        >
          {theme}
        </button>
      </header>
      {!up && (
        <div className="banner" role="status">
          <span
            className="banner-glyph"
            data-kind={connecting ? "connecting" : "lost"}
            aria-hidden="true"
          />
          {connecting ? (
            "CONNECTING to NATS, no data yet"
          ) : (
            <>
              DISCONNECTED{since} ({lastData}
              {retry})
            </>
          )}
        </div>
      )}
      <main className="board" data-disconnected={!up}>
        <div
          role="tabpanel"
          id={`panel-${screen}`}
          aria-labelledby={`tab-${screen}`}
        >
          <h2 className="sr-only">{label}</h2>
          <Screen id={screen} now={now} />
        </div>
      </main>
    </>
  );
}
