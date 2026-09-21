import type { RunPreset } from "@workspace/api-client";

/**
 * The preset picker's pure half.
 *
 * A preset is a SEED. Picking one sets the composer's own controls — effort and
 * the plan toggle — to the preset's values, and the user is then free to change
 * either before sending. Nothing hidden happens at send time that the picker
 * did not already show.
 *
 * APPROVAL MODE IS NOT A COMPOSER CONTROL, and that is why it is not here.
 * It used to be: `applyPreset` returned the preset's `approval_mode`, both
 * callers passed `approvalMode: ""` as the current value so the preset always
 * won, and both then fired `setApprovalMode` — a PUT on the conversation row
 * that every later turn reads. Picking "Quick lookup", described in the picker
 * as "One fast pass: low effort, three short passages, core tools only", moved
 * a thread the user had deliberately put in `plan` mode (writes denied) down to
 * `ask_writes` (writes park and can be approved), and left it there. That is
 * exactly the hidden policy layer `services/run_presets.py` says a preset must
 * not be; the server no longer publishes the field at all.
 *
 * The mapping lives here, in one function, because two callers apply it: the
 * pane's `use-conversation-thread` and the shell's own composer. Two copies of
 * "what picking deep-research does" would drift, and the drift would be
 * invisible — both would still look like they worked.
 */

export type { RunPreset };

/** What the composer holds that a preset can seed. */
export type PresetSeedable = {
  effort: string;
  stepPlan: boolean;
};

/** The router's own row. It is a question, not a policy, so it seeds nothing. */
export const AUTO_PRESET = "auto";

/**
 * The composer state after picking `preset`.
 *
 * Three rules, and each exists for a reason worth keeping:
 *
 * - `auto` returns `current` UNCHANGED. Auto is resolved server-side from the
 *   prompt at send time, so there is nothing to preview; seeding from its
 *   placeholder fields would show the user a policy their turn will not run
 *   under.
 * - An `effort` of "" leaves the effort alone. "" is the identity value —
 *   "whatever you already chose" — not a third level.
 * - Everything else is taken from the preset, because a picker that claimed to
 *   pin a policy and then left half of it unset would not be a policy bundle.
 *   "Everything else" is two visible controls; it does not and must not reach
 *   the thread's approval mode (see the module note).
 */
export function applyPreset(
  preset: RunPreset,
  current: PresetSeedable,
): PresetSeedable {
  if (preset.name === AUTO_PRESET) return current;
  return {
    effort: preset.effort || current.effort,
    stepPlan: preset.step_plan,
  };
}

/** Human labels for the catalogue names, for a chip or a status line. */
const LABELS: Record<string, string> = {
  "": "No preset",
  auto: "Auto",
  "quick-lookup": "Quick lookup",
  "grounded-answer": "Grounded answer",
  "deep-research": "Deep research",
  deliverable: "Deliverable",
};

/**
 * A preset's label, or the name itself when this build does not know it.
 *
 * Passing an unknown name through rather than blanking it is the same
 * degradation the server applies to a retired preset: showing the raw name
 * tells the reader what their thread is set to, and showing nothing tells them
 * their setting vanished.
 */
export function presetLabel(name: string): string {
  return LABELS[name] ?? name;
}
