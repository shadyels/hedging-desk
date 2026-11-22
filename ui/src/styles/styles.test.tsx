import { act, cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, test } from "vitest";
import { BlotterBoard } from "../blotter/BlotterBoard";
import { XScroll } from "../components/XScroll";
import { resetBus } from "../store";
import blotter from "./blotter.css?raw";
import health from "./health.css?raw";
import risk from "./risk.css?raw";
import shell from "./shell.css?raw";
import tokens from "./tokens.css?raw";
import tracker from "./tracker.css?raw";

const SHEETS = { blotter, health, risk, shell, tokens, tracker };
afterEach(cleanup);

/** The declaration block of the first rule whose selector text contains `selector`. */
const block = (css: string, selector: string): string => {
  const i = css.indexOf(selector);
  if (i < 0) throw new Error(`no rule ${selector}`);
  return css.slice(css.indexOf("{", i) + 1, css.indexOf("}", i));
};
/** Every rule, as [selector list, body]. Plain CSS only (no nesting, no at-rule bodies matter here). */
const rules = (css: string): [string[], string][] =>
  [
    ...css.replace(/\/\*[\s\S]*?\*\//g, "").matchAll(/([^{}@]+)\{([^{}]*)\}/g),
  ].map((m) => [(m[1] ?? "").split(",").map((x) => x.trim()), m[2] ?? ""]);

describe("N2: no page-level min-width", () => {
  test("tokens.css has no --page-min-w and body has no min-width; nothing else pins a page width", () => {
    expect(tokens).not.toContain("--page-min-w");
    expect(block(tokens, "body {")).not.toMatch(/min-width/);
    for (const [name, css] of Object.entries(SHEETS))
      expect(css, name).not.toContain("--page-min-w");
  });

  test("a panel is not itself a scroller: each table scrolls in its own .xscroll", () => {
    expect(block(risk, ".panel {")).not.toMatch(/overflow/);
    expect(block(risk, ".xscroll {")).toMatch(/overflow-x:\s*auto/);
    expect(block(blotter, ".blotter .panel {")).not.toMatch(/overflow/);
  });
});

describe("N6: a focus ring on every interactive element and scroller", () => {
  const focus = rules(tokens).filter(([, body]) =>
    /outline:\s*var\(--focus-ring\)/.test(body),
  );

  test("one rule from the tokens covers links, buttons, fields and every focusable element", () => {
    expect(focus).toHaveLength(1);
    const [sels, body] = focus[0] ?? [[], ""];
    expect(sels.every((s) => s.endsWith(":focus-visible"))).toBe(true);
    for (const base of [
      "a[href]",
      "button",
      "input",
      "select",
      "textarea",
      "[tabindex]",
    ])
      expect(sels).toContain(`${base}:focus-visible`);
    expect(body).toMatch(/outline-offset:\s*var\(--focus-offset\)/);
  });

  test("the ring is from tokens only; no stylesheet removes an outline", () => {
    expect(tokens).toMatch(/--focus-ring:\s*2px solid var\(--text\)/);
    for (const [name, css] of Object.entries(SHEETS))
      expect(css, name).not.toMatch(/outline:\s*(none|0)\b/);
  });

  test("scrollers keep the ring inside their edge, where an overflow clip cannot hide it", () => {
    const inset = rules(tokens).find(([sels]) =>
      sels.includes(".xscroll:focus-visible"),
    );
    expect(inset?.[0]).toContain(".bl-scroll:focus-visible");
    expect(inset?.[1]).toMatch(
      /outline-offset:\s*calc\(var\(--space-half\) \* -1\)/,
    );
  });

  test("every interactive element the blotter renders is matched by that rule (controls mount without data)", () => {
    resetBus();
    const { container } = render(<BlotterBoard now={0} />);
    const sels = (focus[0]?.[0] ?? []).map((s) =>
      s.replace(":focus-visible", ""),
    );
    const interactive = container.querySelectorAll(
      "a[href], button, input, select, textarea, [tabindex]",
    );
    expect(interactive.length).toBeGreaterThanOrEqual(5); // four selects and the text field, with no data at all
    for (const el of interactive)
      expect(
        sels.some((s) => el.matches(s)),
        el.outerHTML.slice(0, 60),
      ).toBe(true);
  });
});

describe("N7: selection and scrollbars follow the theme tokens", () => {
  test("::selection uses neutral tokens, so dark and light both follow", () => {
    const b = block(tokens, "::selection");
    expect(b).toMatch(/background:\s*var\(--text-2\)/);
    expect(b).toMatch(/color:\s*var\(--bg\)/);
  });

  test("scrollbar-color is set on the root from tokens", () => {
    expect(tokens).toMatch(
      /:root\s*\{\s*scrollbar-color:\s*var\(--border-strong\)\s+var\(--surface-2\)/,
    );
  });

  test("both themes define every token those two rules read", () => {
    const dark = block(tokens, ':root[data-theme="dark"]');
    const light = block(tokens, ':root[data-theme="light"]');
    for (const t of ["--bg", "--text-2", "--border-strong", "--surface-2"]) {
      expect(dark, `dark ${t}`).toMatch(new RegExp(`${t}:`));
      expect(light, `light ${t}`).toMatch(new RegExp(`${t}:`));
    }
  });
});

describe("XScroll: a table that overflows is a focusable named region", () => {
  const widths = { scroll: 0, client: 0 };
  let observed: (() => void) | null = null;
  const orig = {
    sw: Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollWidth"),
    cw: Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientWidth"),
    ro: globalThis.ResizeObserver,
  };
  const install = () => {
    Object.defineProperty(HTMLElement.prototype, "scrollWidth", {
      configurable: true,
      get: () => widths.scroll,
    });
    Object.defineProperty(HTMLElement.prototype, "clientWidth", {
      configurable: true,
      get: () => widths.client,
    });
    globalThis.ResizeObserver = class {
      constructor(cb: () => void) {
        observed = cb;
      }
      observe() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
  };
  const restore = () => {
    if (orig.sw)
      Object.defineProperty(HTMLElement.prototype, "scrollWidth", orig.sw);
    if (orig.cw)
      Object.defineProperty(HTMLElement.prototype, "clientWidth", orig.cw);
    globalThis.ResizeObserver = orig.ro;
  };

  test("fits: a labelled region with no tab stop; overflows: tabindex 0; fits again: no tab stop", () => {
    install();
    try {
      widths.scroll = 500;
      widths.client = 500;
      const { getByRole } = render(
        <XScroll label="Books table">
          <table />
        </XScroll>,
      );
      const region = () => getByRole("region", { name: "Books table" });
      expect(region().hasAttribute("tabindex")).toBe(false);

      act(() => {
        widths.scroll = 900; // the window narrowed
        observed?.();
      });
      expect(region().getAttribute("tabindex")).toBe("0");
      act(() => {
        widths.scroll = 500; // and widened again
        observed?.();
      });
      expect(region().hasAttribute("tabindex")).toBe(false);
    } finally {
      restore();
    }
  });

  test("a table that overflows on first paint is focusable at once", () => {
    install();
    try {
      widths.scroll = 1400;
      widths.client = 1000;
      const { getByRole } = render(
        <XScroll label="Blotter table">
          <table />
        </XScroll>,
      );
      expect(
        getByRole("region", { name: "Blotter table" }).getAttribute("tabindex"),
      ).toBe("0");
    } finally {
      restore();
    }
  });
});
