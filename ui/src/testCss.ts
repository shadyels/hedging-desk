/**
 * Test-only: jsdom has no cascade for custom properties, so a computed check works on the raw declarations.
 * `loadCss` injects the real stylesheets, imported `?raw` like the registry test does (vitest does not apply CSS imports); `ink` walks up from an
 * element to the first ancestor that sets `color` (jsdom reports `inherit` verbatim); `token` reads a
 * literal from tokens.css so a `var(--x)` declaration can be compared with its resolved value.
 */
import risk from "./styles/risk.css?raw";
import shell from "./styles/shell.css?raw";
import tokens from "./styles/tokens.css?raw";
import tracker from "./styles/tracker.css?raw";

const SHEETS = { risk, shell, tokens, tracker };
const read = (name: keyof typeof SHEETS) => SHEETS[name];

export function loadCss(...names: (keyof typeof SHEETS)[]): () => void {
  const style = document.createElement("style");
  style.textContent = names.map(read).join("\n");
  document.head.appendChild(style);
  return () => style.remove();
}

export function ink(el: Element | null): string {
  for (let e = el; e; e = e.parentElement) {
    const c = getComputedStyle(e).color;
    if (c !== "" && c !== "inherit") return c;
  }
  return "";
}

/** `--fw-bold` -> "600". Dark values win where a token is themed (first declaration). */
export function token(name: string): string {
  const m = new RegExp(`${name}:\\s*([^;]+);`).exec(read("tokens"));
  if (!m?.[1]) throw new Error(`no token ${name}`);
  return m[1].trim();
}
