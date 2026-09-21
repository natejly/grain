import type { Watch } from "@workspace/api-client";

/**
 * Pure label helpers for the Watches view.
 *
 * The one that matters is `SHARED_HELPER_TEXT`. `shared` is the only control
 * in this family that widens memory visibility, so its copy has to say what
 * the server means — not "share this watch", which sounds like read access.
 */

/** "source: report.pdf" or "space: Research". */
export function describeTarget(watch: Watch, name: string): string {
  const label = name.trim() || watch.target_id;
  return `${watch.target_kind}: ${label}`;
}

/** The schedule in the form a person typed it, plus the zone it fires in. */
export function describeSchedule(watch: Watch): string {
  return `${watch.schedule_cron} · ${watch.schedule_timezone}`;
}

/**
 * When this watch last found something, in words that keep the three states
 * apart: never run, ran and never changed, ran and changed.
 *
 * "Checked, no change yet" is a real and good outcome — the watch is working —
 * so it must not read like the "never run" case, which is a configuration
 * problem.
 */
export function describeLastChange(watch: Watch, relative: (iso: string) => string): string {
  if (!watch.last_checked_at) return "Never run";
  if (!watch.last_change_at) return `Checked ${relative(watch.last_checked_at)} — no change yet`;
  return `Last change ${relative(watch.last_change_at)}`;
}

/**
 * THE COPY. A shared watch writes what it learns to `owner_id=""`, which is
 * every member's memory shelf; an unshared one writes to its creator's. Said
 * in the words the server means, because a person ticking this box is granting
 * the only visibility widening this feature can do.
 */
export const SHARED_HELPER_TEXT =
  "A shared watch writes what it learns to everyone’s memory shelf; otherwise it writes to yours.";

/** At most ten declared fields — the server's own cap, echoed in the form. */
export const MAX_EXTRACTION_FIELDS = 10;

/**
 * What a field name may contain, said before the server has to say it.
 *
 * The name becomes the claim key the watch's memory supersedes on, so the
 * server refuses one it cannot slugify (422) rather than filing every fire
 * under a key nobody can read. "price (USD)" is the ordinary way to get this
 * wrong, so the note names the characters rather than the rule.
 */
export const FIELD_NAME_HELPER_TEXT =
  "Letters, digits, spaces and - . : / only — “price (USD)” is refused, “price USD” is fine.";

/**
 * What a `run now` produced, for the line under the button. `changed: false`
 * is reported plainly rather than silently: the watch ran, and that is the
 * fact the person pressed the button to learn.
 */
export function describeRunNow(changed: boolean): string {
  return changed
    ? "Checked — the target changed, and the brief was rewritten."
    : "Checked — nothing has changed, so nothing was written.";
}
