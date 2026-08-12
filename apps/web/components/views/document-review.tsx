"use client";

import type { PendingDocumentEdit, ReviewSegment } from "@workspace/api-client";
import { Check, X } from "lucide-react";
import { useMemo, useState } from "react";

export type HunkDecision = (
  edit: PendingDocumentEdit,
  decision: "approved" | "denied",
  acceptedHunks?: number[],
) => Promise<void>;

/**
 * A proposed edit, reviewed one change at a time, in the document itself.
 *
 * The all-or-nothing card said "the assistant wants to edit this document" and
 * showed a unified diff beside the text it was about. A reviewer's real
 * question is narrower than that card can ask — the second of three changes is
 * wrong and the other two are fine — and answering it meant denying everything
 * and asking again.
 *
 * Two things this deliberately is not:
 *
 *  - It is not a rendered preview. A hunk is a run of *lines*, so accepting one
 *    is a decision about lines, and showing markdown here would mean the thing
 *    you looked at and the thing you accepted were different objects.
 *  - It is not one decision per click. Every choice stages locally and leaves
 *    together as a single `accepted_hunks` approval, because the server turns
 *    that into one version row whose summary counts what was applied. Deciding
 *    hunk by hunk over the wire would write a version per click and leave a
 *    history of partial states no one ever read, none of which is undoable to
 *    anywhere useful.
 */
export function DocumentReview({
  edit,
  decide,
}: {
  edit: PendingDocumentEdit;
  decide: HunkDecision;
}) {
  /**
   * Which hunks the reviewer has turned *off*. Rejection is what is tracked,
   * not acceptance, so the default state is "take the proposal whole" — exactly
   * what the Approve button meant before this existed. Nothing is silently
   * decided by a reviewer who reads the changes and presses Apply, and the
   * count on the button says what will happen at every moment.
   */
  const [rejected, setRejected] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const total = useMemo(
    () => edit.segments.filter((segment) => segment.index >= 0).length,
    [edit.segments],
  );
  const accepted = total - rejected.size;

  function toggle(index: number, take: boolean) {
    setRejected((current) => {
      const next = new Set(current);
      if (take) next.delete(index);
      else next.add(index);
      return next;
    });
  }

  async function apply() {
    setBusy(true);
    setError("");
    const wanted = edit.segments
      .filter((segment) => segment.index >= 0 && !rejected.has(segment.index))
      .map((segment) => segment.index);
    try {
      // Rejecting everything is a denial, not an approval of nothing: the
      // server refuses an empty `accepted_hunks`, and rightly — "approved, and
      // apply none of it" is not a thing a reviewer can have meant.
      if (wanted.length === 0) await decide(edit, "denied");
      else await decide(edit, "approved", wanted);
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not record that decision",
      );
      // Only on failure: a success takes this whole panel off the screen.
      setBusy(false);
    }
  }

  const applyLabel =
    accepted === 0
      ? `Reject ${total === 1 ? "this change" : "all changes"}`
      : `Apply ${accepted} of ${total} change${total === 1 ? "" : "s"}`;

  return (
    <section className="document-review" aria-label="Proposed changes">
      <div className="document-review-bar">
        <strong>
          {total} proposed change{total === 1 ? "" : "s"}
        </strong>
        <span className="field-hint">
          {accepted === total
            ? "All accepted"
            : `${rejected.size} rejected, ${accepted} accepted`}
        </span>
        {error && <span className="tool-error">{error}</span>}
        <button
          type="button"
          className="primary-button"
          disabled={busy}
          onClick={() => void apply()}
        >
          {busy ? "Applying…" : applyLabel}
        </button>
      </div>
      <div className="document-review-body">
        {edit.segments.map((segment, position) => (
          <Stretch
            key={position}
            segment={segment}
            taken={!rejected.has(segment.index)}
            disabled={busy}
            onChoose={(take) => toggle(segment.index, take)}
          />
        ))}
      </div>
    </section>
  );
}

/** One stretch of the document: either untouched text, or a decision. */
function Stretch({
  segment,
  taken,
  disabled,
  onChoose,
}: {
  segment: ReviewSegment;
  taken: boolean;
  disabled: boolean;
  onChoose: (take: boolean) => void;
}) {
  if (segment.index < 0) {
    return (
      <div className="diff">
        {segment.before.map((line, index) => (
          <div key={index} className="diff-line ctx">
            {line || " "}
          </div>
        ))}
      </div>
    );
  }
  // Numbered from one for the reader; `index` stays zero-based because it is
  // the wire value the server accepts by.
  const ordinal = segment.index + 1;
  return (
    <div className={taken ? "review-hunk taken" : "review-hunk left"}>
      <div className="review-hunk-bar">
        <span>Change {ordinal}</span>
        <span className="field-hint">{taken ? "Will apply" : "Will not apply"}</span>
        {/* The ordinal is in the accessible name and not in the visible one.
            On screen the block is already headed "Change 2", so repeating it
            on both buttons only made the bar wrap; to a screen reader working
            through the controls in order there is no heading in earshot, and a
            bare "Accept" would be the fourth indistinguishable one on the page. */}
        <button
          type="button"
          className="ghost-button"
          aria-label={`Accept change ${ordinal}`}
          aria-pressed={taken}
          disabled={disabled}
          onClick={() => onChoose(true)}
        >
          <Check size={13} /> Accept
        </button>
        <button
          type="button"
          className="ghost-button"
          aria-label={`Reject change ${ordinal}`}
          aria-pressed={!taken}
          disabled={disabled}
          onClick={() => onChoose(false)}
        >
          <X size={13} /> Reject
        </button>
      </div>
      {/* Both sides always shown, with the unchosen one dimmed rather than
          removed. A hunk whose halves appear and disappear as you toggle makes
          the block jump under the cursor that is toggling it, and hides the
          text you are deciding *against* at the moment you decide. */}
      <div className="diff">
        {segment.before.map((line, index) => (
          <div key={`before-${index}`} className={taken ? "diff-line del" : "diff-line ctx"}>
            {line || " "}
          </div>
        ))}
        {segment.after.map((line, index) => (
          <div key={`after-${index}`} className={taken ? "diff-line add" : "diff-line dropped"}>
            {line || " "}
          </div>
        ))}
      </div>
    </div>
  );
}
