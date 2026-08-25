/**
 * Where a thread opens when it is *opened* — the palette's Enter, a favorite's
 * click: in the primary pane ("primary", the default) or beside it in a new
 * split pane ("split"). One stored word, flipped from the palette's Preference
 * row; the modifier paths (⌘⏎, the rail's split button) invert whichever way
 * the preference points, so both destinations stay one gesture away.
 */

/** localStorage key for the preference, following the `grain.*` convention. */
export const THREAD_OPEN_KEY = "grain.thread-open";

export type ThreadOpen = "primary" | "split";

/** Decode the stored preference: exactly "split" opts in, anything else —
 *  missing, malformed, hostile — is the default. Total, never a throw. */
export function parseThreadOpen(raw: string | null): ThreadOpen {
  return raw === "split" ? "split" : "primary";
}
