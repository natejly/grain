import type { RunUndoResult } from "@workspace/api-client";

/**
 * Copy for the run-undo affordance, React-free so it can be unit-tested.
 *
 * The summary's job is honesty about the half an undo cannot do: writes whose
 * effects left the workspace come back in `skipped`, and the sentence must
 * name them rather than let "Undone" imply everything was.
 */

/** What the confirm dialog asks before anything is reverted. */
export const UNDO_CONFIRM =
  "Undo this run's changes? Documents, boards and files it wrote are restored " +
  "to their state before the run; effects outside the workspace cannot be " +
  "taken back.";

function count(n: number, noun: string): string {
  return `${n} ${noun}${n === 1 ? "" : "s"}`;
}

/**
 * One sentence for what an undo did. "" when there is nothing worth a notice —
 * everything reverted cleanly — so the caller can show only the surprises.
 */
export function summarizeUndo(result: RunUndoResult): string {
  const { reverted, skipped } = result;
  if (skipped.length === 0) {
    return "";
  }
  const names = skipped.map((item) => item.tool_name).join(", ");
  const head =
    reverted.length > 0
      ? `Undid ${count(reverted.length, "change")}`
      : "Nothing could be undone";
  return `${head}; ${count(skipped.length, "change")} could not be reverted (${names}).`;
}
