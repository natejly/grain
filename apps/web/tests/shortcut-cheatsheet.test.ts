import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ShortcutCheatSheet } from "../components/shortcut-cheatsheet";
import { CHORD_VIEWS, chordHint } from "../components/views/chords";

/**
 * The "?" overlay's one hard promise: it is generated from the real chord
 * table. A chord added to chords.ts without touching the sheet must still
 * render a row here — the drift pin — and the overlay must behave like every
 * other scrimmed dialog (role, name, Escape).
 */

function sheet(props: Partial<{ chordsEnabled: boolean; close: () => void }> = {}) {
  return render(
    createElement(ShortcutCheatSheet, {
      chordsEnabled: true,
      close: () => undefined,
      ...props,
    }),
  );
}

afterEach(cleanup);

describe("ShortcutCheatSheet", () => {
  it("is a dialog named for what it holds", () => {
    sheet();
    expect(screen.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeTruthy();
  });

  it("renders one chord row per CHORD_VIEWS entry — the drift pin", () => {
    const { container } = sheet();
    const rows = container.querySelectorAll(".cheatsheet-chord");
    expect(rows.length).toBe(CHORD_VIEWS.length);
    // Each row carries the real binding in chordHint()'s own format and the
    // chord's label, so the sheet teaches exactly what the listener accepts.
    for (const chord of CHORD_VIEWS) {
      expect(screen.getByText(chordHint(chord.view) as string)).toBeTruthy();
      expect(screen.getByText(chord.label)).toBeTruthy();
    }
  });

  it("dims the chord rows and says so while the kill-switch is off", () => {
    const { container } = sheet({ chordsEnabled: false });
    const dimmed = container.querySelectorAll(".cheatsheet-chord.dimmed");
    expect(dimmed.length).toBe(CHORD_VIEWS.length);
    expect(screen.getByText(/G-chords are off/)).toBeTruthy();
    cleanup();
    // And with the switch on, nothing is dimmed and no note renders.
    const { container: enabled } = sheet({ chordsEnabled: true });
    expect(enabled.querySelectorAll(".cheatsheet-chord.dimmed").length).toBe(0);
    expect(screen.queryByText(/G-chords are off/)).toBeNull();
  });

  it("closes on Escape and on a scrim click, but not on a card click", () => {
    const close = vi.fn();
    const { container } = sheet({ close });
    fireEvent.click(container.querySelector(".palette.cheatsheet") as Element);
    expect(close).not.toHaveBeenCalled();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(close).toHaveBeenCalledTimes(1);
    fireEvent.click(container.querySelector(".palette-scrim") as Element);
    expect(close).toHaveBeenCalledTimes(2);
  });

  it("teaches the tiers above the chords too, '?' itself included", () => {
    sheet();
    expect(screen.getByText("⌘K")).toBeTruthy();
    expect(screen.getByText("⌘\\")).toBeTruthy();
    expect(screen.getByText("?")).toBeTruthy();
    expect(screen.getByText("Shift Enter")).toBeTruthy();
  });
});
