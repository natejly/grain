import type { View } from "./shared";

/**
 * G-chords: press G, then a letter, and land on a destination — the keyboard
 * tier above ⌘K for the five places a session actually lives in. Pure data and
 * predicates, no DOM: the shell owns the listener and the arming timer, and the
 * tests exercise the decisions without one.
 *
 * The letters follow the rail: Chat, Inbox, Library, Automations — plus D for
 * Dashboards, the one shelf inside Library worth its own chord because it is
 * where "show me the numbers" lands. Each maps to the *view* the door opens
 * onto, not the group, so the chord works from anywhere including surfaces
 * with no group of their own.
 */
export const CHORD_VIEWS: ReadonlyArray<{ key: string; view: View; label: string }> = [
  { key: "c", view: "chat", label: "Chat" },
  { key: "i", view: "activity", label: "Inbox" },
  { key: "l", view: "documents", label: "Library" },
  { key: "a", view: "workflows", label: "Automations" },
  { key: "d", view: "dashboards", label: "Dashboards" },
];

/** How long a lone G stays armed before the chord lapses. */
export const CHORD_WINDOW_MS = 1500;

/** localStorage key for the kill-switch, following the `grain.*` convention. */
export const CHORDS_KEY = "grain.chords";

/** Decode the kill-switch: exactly "off" disables, anything else — missing,
 *  malformed, hostile — leaves the chords on. Total, never a throw. */
export function parseChordsEnabled(raw: string | null): boolean {
  return raw !== "off";
}

/** Encode the kill-switch — the inverse of `parseChordsEnabled`. */
export function serializeChordsEnabled(enabled: boolean): string {
  return enabled ? "on" : "off";
}

/** The view a second keypress completes the chord to, or null. */
export function chordTarget(key: string): View | null {
  const hit = CHORD_VIEWS.find((chord) => chord.key === key.toLowerCase());
  return hit ? hit.view : null;
}

/** The "G C"-style hint for a view, or null for views without a chord. */
export function chordHint(view: View): string | null {
  const hit = CHORD_VIEWS.find((chord) => chord.view === view);
  return hit ? `G ${hit.key.toUpperCase()}` : null;
}

/**
 * Whether a keydown belongs to typing rather than navigation. A chord that
 * fires while someone writes "going to lunch" into the composer would yank the
 * view mid-sentence — so anything that edits text swallows the letters.
 */
export function isTypingContext(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
  return target.isContentEditable;
}

/**
 * Whether a keydown may take part in a chord at all: the kill-switch is on,
 * a bare letter, no modifiers (⌘G and ctrl+letter belong to the browser), not
 * typed into a field. The palette closes over its own keys before this is
 * consulted. `enabled` is the first argument rather than a shell-side guard so
 * the switch and the key rules cannot be consulted separately and disagree.
 */
export function chordEligible(
  enabled: boolean,
  event: {
    key: string;
    metaKey: boolean;
    ctrlKey: boolean;
    altKey: boolean;
    target: EventTarget | null;
  },
): boolean {
  if (!enabled) return false;
  if (event.metaKey || event.ctrlKey || event.altKey) return false;
  if (event.key.length !== 1) return false;
  return !isTypingContext(event.target);
}
