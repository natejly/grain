"use client";

/**
 * Collapsing the panes on the left, so the thing you are working on can have the
 * whole window.
 *
 * Three surfaces use this — the workspace rail, the document file list, the
 * project file tree — and they share more than a CSS class: the state has to
 * survive a reload (a rail you collapse every morning is a rail you collapse
 * every morning), and the control has to be reachable and legible after it has
 * hidden its own panel. The pure parts live at the top so the storage round trip
 * and, more importantly, the accessible name are testable without a DOM.
 */

import { PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { useEffect, useState } from "react";

/**
 * localStorage key for one pane's collapsed state, following the workspace
 * switcher's `grain.*` convention.
 *
 * One key per pane rather than one key holding a set: two of these are mounted
 * at once (the rail and whichever list pane the open view has), and a shared key
 * would make each of them write back a value it read before the other's change —
 * the last toggle silently reverting the previous one.
 */
export function collapseKey(pane: string): string {
  return `grain.collapsed.${pane}`;
}

/**
 * localStorage key for one sidebar section's collapsed state — the sibling of
 * `collapseKey` for the shelves *inside* a destination's sidebar. Keyed by the
 * section's stable id (navigation.ts), not its label, so rewording a heading
 * does not forget who folded it away.
 */
export function sectionCollapseKey(groupId: string, sectionId: string): string {
  return `grain.section.${groupId}.${sectionId}`;
}

/**
 * The one total read both key families share: anything other than the stored
 * "1" — absent, hand-edited, or unreadable because the origin blocks storage —
 * is "not collapsed", which is the state everything here ships in.
 */
function readFlag(key: string): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(key) === "1";
  } catch {
    // Private mode or a blocked origin: the pane still collapses for this
    // session, it just will not remember.
    return false;
  }
}

function persistFlag(key: string, collapsed: boolean): void {
  if (typeof window === "undefined") return;
  try {
    if (collapsed) window.localStorage.setItem(key, "1");
    else window.localStorage.removeItem(key);
  } catch {
    // As above: the toggle still applies to this tab.
  }
}

/** Read one pane's persisted state. */
export function readCollapsed(pane: string): boolean {
  return readFlag(collapseKey(pane));
}

export function persistCollapsed(pane: string, collapsed: boolean): void {
  persistFlag(collapseKey(pane), collapsed);
}

/** Read one sidebar section's persisted state. */
export function readSectionCollapsed(groupId: string, sectionId: string): boolean {
  return readFlag(sectionCollapseKey(groupId, sectionId));
}

export function persistSectionCollapsed(
  groupId: string,
  sectionId: string,
  collapsed: boolean,
): void {
  persistFlag(sectionCollapseKey(groupId, sectionId), collapsed);
}

/**
 * What the toggle is called, which is the state pressing it puts you in.
 *
 * "Sidebar" as a name tells a screen-reader user what the button is next to,
 * not what it does, and a name that does not change leaves them pressing it to
 * find out which way it went. `aria-expanded` carries the current state; the
 * name carries the outcome, and the two must not contradict each other — hence
 * one function rather than a string at each call site.
 */
export function collapseLabel(subject: string, collapsed: boolean): string {
  return collapsed ? `Show the ${subject}` : `Hide the ${subject}`;
}

/**
 * A pane's collapsed state, remembered across reloads.
 *
 * The initial read happens in the `useState` initialiser rather than a mount
 * effect, so a collapsed pane is never painted open for a frame first. That is
 * safe here for the reason it is safe for the chat split: nothing in this tree
 * is server-rendered — the auth gate shows a splash until bootstrap resolves in
 * the browser — so there is no hydration pass to mismatch.
 */
export function useCollapsiblePane(pane: string): [boolean, () => void] {
  const [collapsed, setCollapsed] = useState(() => readCollapsed(pane));
  useEffect(() => {
    persistCollapsed(pane, collapsed);
  }, [pane, collapsed]);
  return [collapsed, () => setCollapsed((value) => !value)];
}

export type PaneToggleProps = {
  /** What the pane is, in the middle of a sentence: "the file list". */
  subject: string;
  collapsed: boolean;
  toggle: () => void;
  /** The id of the element this shows and hides, for `aria-controls`. */
  controls: string;
};

/**
 * The button that collapses a pane and the button that brings it back — one
 * control, in one place, that never hides itself. It stays inside the pane
 * (which shrinks to a strip around it) or in the chrome beside it, but never in
 * the part that disappears: a control you have to restore the pane to reach is
 * not a control.
 */
export function PaneToggle({ subject, collapsed, toggle, controls }: PaneToggleProps) {
  const Icon = collapsed ? PanelLeftOpen : PanelLeftClose;
  return (
    <button
      type="button"
      className="icon-button pane-toggle"
      onClick={toggle}
      aria-expanded={!collapsed}
      aria-controls={controls}
      aria-label={collapseLabel(subject, collapsed)}
    >
      <Icon size={16} aria-hidden="true" />
    </button>
  );
}
