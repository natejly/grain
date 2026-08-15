"use client";

import type { PendingDocumentEdit } from "@workspace/api-client";
import { Check, GitPullRequestArrow, X } from "lucide-react";
import { useState } from "react";
import { ProposalDiff } from "./proposal-diff";

/**
 * One agent write parked on approval, from GET /api/documents-pending.
 *
 * Re-exported rather than redeclared. It was a second, structurally identical
 * copy of the client's type until the server started sending `segments` — at
 * which point the copy was simply wrong, and every consumer that imported it
 * from here could not see the field the inline review is built from.
 */
export type { PendingDocumentEdit };

export type PendingDecision = (
  edit: PendingDocumentEdit,
  decision: "approved" | "denied",
) => Promise<void>;

/**
 * A parked edit, shown where the document is rather than only in chat.
 * Deciding here goes through the same approval endpoint the chat card uses, so
 * the run resumes and the write happens exactly once.
 */
function PendingEditCard({
  edit,
  decide,
}: {
  edit: PendingDocumentEdit;
  decide: PendingDecision;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function choose(decision: "approved" | "denied") {
    setBusy(true);
    setError("");
    try {
      await decide(edit, decision);
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not record that decision",
      );
      // Only re-enable on failure: a success removes this card outright.
      setBusy(false);
    }
  }

  const verb = edit.name === "create_document" ? "create" : "edit";
  const target = edit.title ? `“${edit.title}”` : "this document";
  return (
    <div className="tool-card proposed">
      <div className="tool-card-head">
        <GitPullRequestArrow size={14} />
        <span>
          The assistant wants to {verb} {target}
        </span>
      </div>
      <ProposalDiff preview={edit.proposal_preview || "(no preview available)"} />
      {error && <div className="tool-error">{error}</div>}
      <div className="tool-card-approval">
        <div className="tool-card-actions">
          <button
            type="button"
            className="ghost-button"
            disabled={busy}
            onClick={() => void choose("denied")}
          >
            <X size={14} /> Deny
          </button>
          <button
            type="button"
            className="primary-button"
            disabled={busy}
            onClick={() => void choose("approved")}
          >
            <Check size={14} /> {busy ? "Applying…" : "Approve"}
          </button>
        </div>
      </div>
    </div>
  );
}

export function PendingEditList({
  edits,
  decide,
}: {
  edits: PendingDocumentEdit[];
  decide: PendingDecision;
}) {
  if (edits.length === 0) return null;
  return (
    <div className="document-pending">
      {edits.map((edit) => (
        <PendingEditCard key={edit.id} edit={edit} decide={decide} />
      ))}
    </div>
  );
}
