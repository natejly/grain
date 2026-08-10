"use client";

import { Check, GitPullRequestArrow, X } from "lucide-react";
import { useState } from "react";

/** One agent write parked on approval, from GET /api/documents-pending. */
export type PendingDocumentEdit = {
  id: string;
  run_id: string;
  name: string;
  /** Empty for a create, or when the model named a document that doesn't exist. */
  document_id: string;
  title: string;
  proposal_preview: string;
  created_at: string;
};

export type PendingDecision = (
  edit: PendingDocumentEdit,
  decision: "approved" | "denied",
) => Promise<void>;

/**
 * Render a unified diff with per-line colouring. Anything that isn't a diff
 * falls through as plain text. This is the same markup the chat approval card
 * uses, so a proposal reads identically wherever the user meets it.
 */
export function ProposalDiff({ preview }: { preview: string }) {
  const lines = preview.split("\n");
  const isDiff = lines.some((line) => line.startsWith("@@"));
  if (!isDiff) {
    return <div className="proposal-note">{preview}</div>;
  }
  return (
    <div className="diff">
      {lines.map((line, index) => {
        let kind = "ctx";
        if (line.startsWith("+++") || line.startsWith("---")) kind = "file";
        else if (line.startsWith("@@")) kind = "hunk";
        else if (line.startsWith("+")) kind = "add";
        else if (line.startsWith("-")) kind = "del";
        return (
          <div key={index} className={`diff-line ${kind}`}>
            {line || " "}
          </div>
        );
      })}
    </div>
  );
}

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
