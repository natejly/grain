"use client";

import { Moon, Sun } from "lucide-react";
import { setTheme, useTheme } from "./theme";

/**
 * Two-position toggle. There is a third state — "no stored choice, follow the
 * system" — but it is the default rather than something to click into, so the
 * control shows what one press would do instead of exposing a tri-state.
 */
export function ThemeToggle({ className }: { className?: string }) {
  const theme = useTheme();
  const next = theme === "dark" ? "light" : "dark";

  return (
    <button
      type="button"
      className={className || "icon-button"}
      onClick={() => setTheme(next)}
      title={`Switch to ${next} theme`}
      aria-label={`Switch to ${next} theme`}
    >
      {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
    </button>
  );
}
