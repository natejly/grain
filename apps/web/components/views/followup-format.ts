import type { Followup } from "@workspace/api-client";

/**
 * Pure helpers behind the follow-up chips, so the one decision that can do
 * harm is testable without React.
 *
 * THE DECISION: a chip SEEDS the composer; it never sends. A chip that sent
 * would let a stray click spend a turn, and the whole point of the feature is
 * that the next question is cheap to ask — not that it is asked for you.
 */

/** The hover text: where the chip came from, and that it was checked. */
export function describeFollowup(followup: Followup): string {
  const origin =
    followup.origin === "kg"
      ? "From the knowledge graph"
      : "From a heading in the answer";
  const passages =
    followup.chunk_ids.length === 1
      ? "1 passage"
      : `${followup.chunk_ids.length} passages`;
  return `${origin} — ${passages} in this workspace can answer it.`;
}

/**
 * The chips in render order: knowledge-graph chips first.
 *
 * A graph neighbour is a claim about the CORPUS; a heading is a claim about
 * the ANSWER, and an answer can name things the corpus never covered. The
 * server already returns them in this order — this re-states it rather than
 * relying on it, because the ordering is what makes the chip list stable
 * between two runs and stability is what the eval gate measures.
 *
 * Ties break on `probe_score` descending and then on `text`, so the order is
 * total and no two renders of one payload can differ.
 */
export function orderFollowups(followups: Followup[]): Followup[] {
  const rank = (followup: Followup) => (followup.origin === "kg" ? 0 : 1);
  return [...followups].sort(
    (left, right) =>
      rank(left) - rank(right) ||
      right.probe_score - left.probe_score ||
      left.text.localeCompare(right.text),
  );
}

/**
 * What the composer should hold after a chip is clicked.
 *
 * APPENDS rather than clobbers when a draft is already in flight. Losing typed
 * text to a stray chip click is the failure mode that would get the feature
 * turned off, and per-thread drafts are remembered across a thread switch — so
 * the text being replaced may be minutes old and not on screen.
 */
export function seededDraft(current: string, text: string): string {
  const draft = current.trimEnd();
  return draft ? `${draft}\n${text}` : text;
}
