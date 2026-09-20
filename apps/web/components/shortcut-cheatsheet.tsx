"use client";

import { useEffect } from "react";
import { CHORD_VIEWS, chordHint } from "./views/chords";

/**
 * The "?" overlay: every keyboard tier the shell listens for, on one card.
 *
 * The chord rows are MAPPED from `CHORD_VIEWS` rather than written out, so a
 * chord added to chords.ts renders here without anyone remembering to — the
 * sheet cannot drift from the real bindings. The kill-switch state rides in as
 * a prop: with chords off the rows stay visible but dimmed, because "these
 * exist and you turned them off" is the honest reading of the preference, and
 * hiding them would make the palette's toggle look like it deleted a feature.
 */
export function ShortcutCheatSheet({
  chordsEnabled,
  close,
}: {
  chordsEnabled: boolean;
  close: () => void;
}) {
  // Escape closes, capture phase like the "?" opener so nothing below (the
  // composer, a focused row) swallows it while the overlay is up.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      close();
    };
    window.addEventListener("keydown", onKey, { capture: true });
    return () => window.removeEventListener("keydown", onKey, { capture: true });
  }, [close]);

  const row = (keys: string, label: string, dimmed = false) => (
    <div className={dimmed ? "cheatsheet-row dimmed" : "cheatsheet-row"}>
      <kbd>{keys}</kbd>
      <span>{label}</span>
    </div>
  );

  return (
    <div className="palette-scrim" onClick={close}>
      <div
        className="palette cheatsheet"
        role="dialog"
        aria-label="Keyboard shortcuts"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="cheatsheet-section">
          <h2>Navigate</h2>
          {row("⌘K", "Command palette — jump to, create, or find a thread")}
          {row("⌘\\", "Cycle focus through the split's panes")}
          {/* Data-driven from the real chord table — see the module comment. */}
          {CHORD_VIEWS.map((chord) => (
            <div
              key={chord.key}
              className={
                chordsEnabled
                  ? "cheatsheet-row cheatsheet-chord"
                  : "cheatsheet-row cheatsheet-chord dimmed"
              }
            >
              <kbd>{chordHint(chord.view)}</kbd>
              <span>{chord.label}</span>
            </div>
          ))}
          {!chordsEnabled && (
            <p className="cheatsheet-note">
              G-chords are off — turn them back on from the ⌘K palette.
            </p>
          )}
        </div>
        <div className="cheatsheet-section">
          <h2>Composer</h2>
          {row("Enter", "Send the message")}
          {row("Shift Enter", "New line")}
          {row("/", "Skills and commands")}
          {row("Esc", "Close a picker or overlay")}
        </div>
        <div className="cheatsheet-section">
          <h2>Help</h2>
          {row("?", "This cheat sheet")}
        </div>
      </div>
    </div>
  );
}
