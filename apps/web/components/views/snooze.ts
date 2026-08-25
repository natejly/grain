/**
 * Inbox snooze: the pure rules behind "Later". Kept apart from the view so
 * every transition — parse, filter, add, remove, and the wake-time arithmetic
 * — is exercised without a DOM, and each function is total: the persisted
 * value is one hand-editable localStorage key away from anything at all, and
 * a broken snooze map must not take the queue down with it.
 *
 * A snooze hides a row from the nag surfaces (the approvals tab, the sidebar's
 * waiting strip) until its wake time. It is deliberately NOT a fact about the
 * request itself: the rail badge keeps counting a snoozed approval, because
 * the badge answers "how many requests are waiting on a human" and sleeping
 * through one does not make it stop waiting.
 */

/** localStorage key for the snooze map, following the `grain.*` convention. */
export const SNOOZE_KEY = "grain.inbox-snooze";

/** Approval id → ISO-8601 wake time. */
export type SnoozeMap = Record<string, string>;

/**
 * Decode a persisted map. A missing, malformed, or hostile value is an empty
 * map, never a throw; entries survive only when the key is a string and the
 * value parses as a real date — anything else is dropped one entry at a time
 * rather than poisoning the rest.
 */
export function parseSnoozes(raw: string | null): SnoozeMap {
  if (!raw) return {};
  try {
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      return {};
    }
    const map: SnoozeMap = {};
    for (const [id, until] of Object.entries(parsed)) {
      if (typeof until !== "string") continue;
      if (!Number.isFinite(Date.parse(until))) continue;
      map[id] = until;
    }
    return map;
  } catch {
    return {};
  }
}

/**
 * The ids still asleep at `now`. An expired snooze simply stops matching —
 * the row returns to the queue on the next render with no cleanup step to
 * forget, and the boundary is exclusive: a snooze wakes AT its wake time.
 */
export function snoozedIds(map: SnoozeMap, now: Date): Set<string> {
  const asleep = new Set<string>();
  for (const [id, until] of Object.entries(map)) {
    if (Date.parse(until) > now.getTime()) asleep.add(id);
  }
  return asleep;
}

/** A new map with `id` asleep until `until`; the input is left untouched. */
export function addSnooze(map: SnoozeMap, id: string, until: Date): SnoozeMap {
  return { ...map, [id]: until.toISOString() };
}

/** A new map without `id`; the input is left untouched. */
export function removeSnooze(map: SnoozeMap, id: string): SnoozeMap {
  const { [id]: _dropped, ...rest } = map;
  return rest;
}

/**
 * The map with the dead weight gone: entries whose approval is no longer in
 * the waiting set (decided, cancelled) and entries already woken. Without this
 * the key grows one dead pair per snooze forever — the waiting set is the
 * authority on which ids can still matter, and an expired snooze has already
 * done everything it will ever do. Returns the SAME object when nothing
 * changed, so a caller can cheaply skip the write-back.
 */
export function pruneSnoozes(
  map: SnoozeMap,
  waitingIds: Set<string>,
  now: Date,
): SnoozeMap {
  const kept: SnoozeMap = {};
  let dropped = false;
  for (const [id, until] of Object.entries(map)) {
    if (!waitingIds.has(id) || Date.parse(until) <= now.getTime()) {
      dropped = true;
      continue;
    }
    kept[id] = until;
  }
  return dropped ? kept : map;
}

/**
 * Tomorrow at 09:00 local — always tomorrow, even at 07:00, because "later"
 * pressed in the morning means "not today". Built with setDate/setHours so a
 * DST change in between lands on the wall-clock nine, not nine-hours-later.
 */
export function nextMorning(now: Date): Date {
  const wake = new Date(now);
  wake.setDate(wake.getDate() + 1);
  wake.setHours(9, 0, 0, 0);
  return wake;
}
