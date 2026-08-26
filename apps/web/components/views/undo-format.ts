import type { RunUndoResult } from "@workspace/api-client";

/**
 * Copy for the run-undo affordance, React-free so it can be unit-tested.
 *
 * The summary's job is honesty about the half an undo cannot do — and about
 * *which* half. A write whose effects left the workspace was never undoable; a
 * restore the clobber guard refused is the undo working exactly as designed,
 * declining to destroy edits made after the run, and can be retried once those
 * edits are settled; a restore that raised is the only one of the three that is
 * a failure. Naming the tool without its reason flattens all three into "it
 * did not work", so every skip is reported with the sentence the service gave
 * it, and `failed` tells the caller which banner it belongs in.
 */

/** What the confirm dialog asks before anything is reverted. */
export const UNDO_CONFIRM =
  "Undo this run's changes? Documents, boards and files it wrote are restored " +
  "to their state before the run; effects outside the workspace cannot be " +
  "taken back.";

function count(n: number, noun: string): string {
  return `${n} ${noun}${n === 1 ? "" : "s"}`;
}

export type UndoOutcome = {
  /** The line to show; "" when there is nothing worth saying — everything
   * reverted cleanly, so the caller shows only the surprises. */
  text: string;
  /** True only when a restore genuinely failed. Protective skips, external
   * effects and rows a concurrent undo already consumed are outcomes to read,
   * not errors, and must never reach the red banner. */
  failed: boolean;
  /** True when undoing the run again could still finish the job. */
  retryable: boolean;
};

/** What one undo actually did, and how loudly to say it. */
export function summarizeUndo(result: RunUndoResult): UndoOutcome {
  const { reverted, skipped } = result;
  if (skipped.length === 0) {
    return { text: "", failed: false, retryable: false };
  }
  const failed = skipped.some((item) => item.outcome === "failed");
  const retryable = skipped.some((item) => item.outcome === "protected");
  const head =
    reverted.length > 0
      ? `Undid ${count(reverted.length, "change")}`
      : "Nothing was undone";
  const details = skipped
    .map((item) => `${item.tool_name}: ${item.reason}`)
    .join(" · ");
  const tail = retryable
    ? " Undo the run again once those edits are settled."
    : "";
  return {
    text: `${head}; ${count(skipped.length, "change")} not reverted — ${details}.${tail}`,
    failed,
    retryable,
  };
}
