import { useCallback, useEffect, useState } from "react";

/** The four screens, in tab order. The id is the URL hash (`#risk`). */
export const SCREENS = [
  { id: "risk", label: "Risk" },
  { id: "tracker", label: "Tracker" },
  { id: "blotter", label: "Blotter" },
  { id: "health", label: "Health" },
] as const;
export type ScreenId = (typeof SCREENS)[number]["id"];

/** An unknown or empty hash is the risk board. */
export const screenFromHash = (hash: string): ScreenId =>
  SCREENS.find((s) => `#${s.id}` === hash)?.id ?? "risk";

/** The active screen, kept in the URL hash so a refresh (and the back button) return to it. */
export function useScreen(): [ScreenId, (id: ScreenId) => void] {
  const [id, setId] = useState(() => screenFromHash(window.location.hash));
  useEffect(() => {
    const sync = () => setId(screenFromHash(window.location.hash));
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, []);
  const go = useCallback((next: ScreenId) => {
    setId(next); // hashchange arrives later; the tab must not wait for it
    window.location.hash = next;
  }, []);
  return [id, go];
}

export type Theme = "dark" | "light";
const THEME_KEY = "hedging-ui.theme";

function storedTheme(): Theme {
  try {
    return window.localStorage.getItem(THEME_KEY) === "light"
      ? "light"
      : "dark";
  } catch {
    return "dark"; // storage can be blocked; dark is the default
  }
}

/** Dark by default; light is `data-theme="light"` on the root element (DESIGN.md "Accessibility"). */
export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(storedTheme);
  useEffect(() => {
    document.documentElement.dataset["theme"] = theme;
    try {
      window.localStorage.setItem(THEME_KEY, theme);
    } catch {
      /* per-viewer convenience only */
    }
  }, [theme]);
  const toggle = useCallback(
    () => setTheme((t) => (t === "dark" ? "light" : "dark")),
    [],
  );
  return [theme, toggle];
}
