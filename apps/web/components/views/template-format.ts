/**
 * Pure naming logic for "Save as template", shared by spaces and workflows.
 *
 * Template names are unique per workspace (the server answers 409 on a
 * collision), and the affordance is a one-click button rather than a form —
 * so the client picks a free name up front instead of bouncing the click off
 * a conflict the user never typed. React-free on purpose, with a mirror test
 * in tests/template-format.test.ts.
 */

/** How the one-click save names a template made from `sourceName`. */
export function templateBaseName(sourceName: string): string {
  const trimmed = sourceName.trim();
  return trimmed ? `${trimmed} template` : "Template";
}

/**
 * The first name in "base", "base 2", "base 3"... that `taken` does not hold.
 *
 * Comparison is case-insensitive because the server's uniqueness effectively
 * is too for anything a person would notice: offering "playbook" beside
 * "Playbook" reads as a duplicate even where the constraint allows it.
 * Names are also capped at the server's 160-character column, counting the
 * suffix, so a long source name cannot push the tiebreaker off the end.
 */
export function nextTemplateName(base: string, taken: string[]): string {
  const held = new Set(taken.map((name) => name.trim().toLowerCase()));
  const fit = (suffix: string) => (base.trim() || "Template").slice(0, 160 - suffix.length) + suffix;
  const first = fit("");
  if (!held.has(first.toLowerCase())) return first;
  for (let ordinal = 2; ; ordinal += 1) {
    const candidate = fit(` ${ordinal}`);
    if (!held.has(candidate.toLowerCase())) return candidate;
  }
}
