import { useLayoutEffect, useRef, useState, type ReactNode } from "react";

/**
 * Horizontal scroller for one table. A table that fits is a plain labelled region; one that does not (a narrow
 * window, a long value) becomes a focusable scroller, so a keyboard user can reach the hidden columns.
 */
export function XScroll({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [overflows, setOverflows] = useState(false);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const check = () => setOverflows(el.scrollWidth > el.clientWidth);
    check();
    if (typeof ResizeObserver === "undefined") return;
    // The scroller resizes with the window; its table resizes with its content.
    const ro = new ResizeObserver(check);
    ro.observe(el);
    if (el.firstElementChild) ro.observe(el.firstElementChild);
    return () => ro.disconnect();
  }, []);
  return (
    <div
      ref={ref}
      className="xscroll"
      role="region"
      aria-label={label}
      tabIndex={overflows ? 0 : undefined}
    >
      {children}
    </div>
  );
}
