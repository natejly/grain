import type { DeliverableManifest } from "@workspace/api-client";

/**
 * How a manifest describes what it cost, and why it stopped.
 *
 * Pure, and separate from the view for the same reason `watch-format.ts` is:
 * this is the copy that tells somebody their cost control worked, and it is
 * the copy that must not say so when it did not.
 *
 * THE STATUS DOES NOT NAME THE REASON. `partial` is written by every terminal
 * halt — a budget running out, a cancellation, a failed node, invalid inputs —
 * so rendering "budget exhausted" for all of them tells a person who pressed
 * Cancel that their budget ran out, and tells a person whose budget really did
 * run out nothing they can trust. The numbers beside it are what distinguish
 * them: the budgets and the spend are both recorded on the manifest now, so
 * the reason is derivable rather than guessed.
 */
export function describeManifestStatus(manifest: DeliverableManifest): string {
  if (manifest.status !== "partial") return "complete";
  if (
    manifest.budget_tool_calls > 0 &&
    manifest.spent_tool_calls >= manifest.budget_tool_calls
  ) {
    return "tool-call budget exhausted — partial results";
  }
  if (
    manifest.budget_seconds > 0 &&
    manifest.spent_seconds >= manifest.budget_seconds
  ) {
    return "time budget exhausted — partial results";
  }
  return "stopped early — partial results";
}

/** "12 of 40 tool calls · 47s of 900s". "—" is a budget of 0: no limit. */
export function describeSpend(manifest: DeliverableManifest): string {
  const calls = `${manifest.spent_tool_calls} of ${
    manifest.budget_tool_calls || "—"
  } tool calls`;
  const seconds = `${manifest.spent_seconds}s of ${
    manifest.budget_seconds ? `${manifest.budget_seconds}s` : "—"
  }`;
  return `${calls} · ${seconds}`;
}
