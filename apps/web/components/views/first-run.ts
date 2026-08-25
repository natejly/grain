/**
 * First-run marks: the pure rules behind "shown once, ever". Kept apart from
 * the views for the snooze module's reason — every transition is exercised
 * without a DOM, and each function is total: the persisted value is one
 * hand-editable localStorage key away from anything at all, and a broken mark
 * list must not take a teaching caption down with it (in either direction —
 * a throw here would either nag forever or never teach at all).
 *
 * One key holds every mark, deliberately: each new first-run moment is a
 * string id in a shared set, not a new `grain.*` key to invent, migrate, and
 * forget to clear.
 */

/** localStorage key for the seen-mark set, following the `grain.*` convention. */
export const FIRST_RUN_KEY = "grain.seen";

/**
 * Decode a persisted mark set. A missing, malformed, or hostile value is an
 * empty set, never a throw; entries survive only when they are strings —
 * anything else is dropped one entry at a time rather than poisoning the rest.
 */
export function parseSeen(raw: string | null): Set<string> {
  if (!raw) return new Set();
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return new Set();
    return new Set(parsed.filter((id): id is string => typeof id === "string"));
  } catch {
    return new Set();
  }
}

/** Encode a mark set the way `parseSeen` reads it back. */
export function serializeSeen(seen: Set<string>): string {
  return JSON.stringify([...seen]);
}

/**
 * Whether `id` has already had its one showing. False on the server and false
 * when storage itself refuses (private mode, quota lockouts): the failure mode
 * of a storage error is showing the caption again, which beats never showing
 * it — or crashing the transcript over a preference.
 */
export function hasSeen(id: string): boolean {
  if (typeof window === "undefined") return false;
  try {
    return parseSeen(window.localStorage.getItem(FIRST_RUN_KEY)).has(id);
  } catch {
    return false;
  }
}

/** Record that `id` has had its showing. A storage refusal is swallowed. */
export function markSeen(id: string): void {
  if (typeof window === "undefined") return;
  try {
    const seen = parseSeen(window.localStorage.getItem(FIRST_RUN_KEY));
    seen.add(id);
    window.localStorage.setItem(FIRST_RUN_KEY, serializeSeen(seen));
  } catch {
    // Nothing to do: the next session simply teaches once more.
  }
}
