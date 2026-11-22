import type { KeyboardEvent } from "react";
import { SCREENS, type ScreenId } from "./nav";

/** WAI-ARIA tabs with automatic activation: arrow keys move focus and select, Home/End jump. */
export function TabBar({
  active,
  onSelect,
}: {
  active: ScreenId;
  onSelect: (id: ScreenId) => void;
}) {
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const i = SCREENS.findIndex((s) => s.id === active);
    const next =
      e.key === "ArrowRight"
        ? (i + 1) % SCREENS.length
        : e.key === "ArrowLeft"
          ? (i + SCREENS.length - 1) % SCREENS.length
          : e.key === "Home"
            ? 0
            : e.key === "End"
              ? SCREENS.length - 1
              : -1;
    const target = SCREENS[next];
    if (!target) return;
    e.preventDefault();
    onSelect(target.id);
    document.getElementById(`tab-${target.id}`)?.focus();
  };
  return (
    <div
      className="tabs"
      role="tablist"
      aria-label="Screens"
      onKeyDown={onKeyDown}
    >
      {SCREENS.map((s) => (
        <button
          key={s.id}
          type="button"
          role="tab"
          id={`tab-${s.id}`}
          className="tab"
          aria-selected={s.id === active}
          aria-controls={`panel-${s.id}`}
          tabIndex={s.id === active ? 0 : -1}
          onClick={() => onSelect(s.id)}
        >
          {s.label}
        </button>
      ))}
    </div>
  );
}
