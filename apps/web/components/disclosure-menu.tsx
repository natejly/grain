"use client";

import { useCallback, useRef, useState, type ReactNode } from "react";

/**
 * The one popover the shell uses: the workspace switcher, "+ Create" and
 * "Settings" are the same object in three places, so they are the same
 * component rather than three near-copies that drift.
 *
 * It is a disclosure of plain buttons in a named group, deliberately not
 * `role="menu"`. A menu owes its users arrow-key navigation and a roving
 * tabindex; a half-implemented one announces "menu" and then behaves like
 * nothing of the sort, which is worse for a screen reader than buttons that
 * simply work with Tab and Enter. What it does owe them, and does here:
 *
 *   - the trigger says whether it is expanded and what it controls,
 *   - Escape closes it and returns focus to the trigger, so the keyboard user
 *     is not left standing where the panel used to be,
 *   - a click anywhere outside closes it.
 */
export type DisclosureMenuProps = {
  /** Ties the trigger to the panel through aria-controls; must be unique. */
  id: string;
  /** Accessible name of the trigger — say the purpose, not just the value. */
  triggerLabel: string;
  /** What the trigger renders. */
  trigger: ReactNode;
  triggerClassName?: string;
  /** Accessible name of the group of buttons inside the panel. */
  menuLabel: string;
  /** Accessible name for the click-catching scrim. */
  closeLabel?: string;
  /** Extra classes on the positioning wrapper, e.g. "stretch". */
  className?: string;
  /** Rendered inside the panel; `close` dismisses it after an action. */
  children: (close: () => void) => ReactNode;
};

export function DisclosureMenu({
  id,
  triggerLabel,
  trigger,
  triggerClassName,
  menuLabel,
  closeLabel,
  className,
  children,
}: DisclosureMenuProps) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);

  // Escape has to put focus back, not just hide the panel: the element the user
  // was on is about to stop existing, and focus would fall to <body>.
  const dismiss = useCallback((restoreFocus: boolean) => {
    setOpen(false);
    if (restoreFocus) triggerRef.current?.focus();
  }, []);

  return (
    <div
      className={className ? `disclosure ${className}` : "disclosure"}
      onKeyDown={(event) => {
        if (event.key !== "Escape" || !open) return;
        event.stopPropagation();
        dismiss(true);
      }}
    >
      <button
        ref={triggerRef}
        className={triggerClassName}
        aria-expanded={open}
        aria-controls={id}
        aria-label={triggerLabel}
        onClick={() => setOpen((value) => !value)}
      >
        {trigger}
      </button>

      {open && (
        <>
          {/* Catches the click that closes the panel. Transparent on purpose:
              this is not a modal, and dimming the app for a five-item list
              would say that it is. */}
          <button
            className="disclosure-scrim"
            aria-label={closeLabel ?? `Close ${menuLabel}`}
            onClick={() => dismiss(false)}
          />
          <div className="disclosure-menu" id={id} role="group" aria-label={menuLabel}>
            {children(() => dismiss(false))}
          </div>
        </>
      )}
    </div>
  );
}
