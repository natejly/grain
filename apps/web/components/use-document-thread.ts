"use client";

import { useCallback } from "react";
import { useSubjectThread } from "./use-subject-thread";

export type DocumentThreadDeps = {
  /** The open document, or "" when none is. */
  documentId: string;
  /** The agent to run as, from bootstrap. */
  agentId?: string;
  /** Re-read the open document and its versions after a write lands. */
  reloadDocument: () => Promise<void>;
  /** Re-read `GET /api/documents-pending`, which the inline review renders. */
  refreshPendingEdits: () => Promise<void>;
};

/**
 * The document editor's chat panel, in terms of the general subject thread.
 *
 * What is left here is the one thing that is genuinely the document's: the
 * ORDER two refreshes happen in after a run settles. Everything else — the
 * get-or-create, the stale-response guard, the stream, the approvals — is
 * `useSubjectThread`, shared with the project and dashboard panels so a fix to
 * any of them is a fix to all three.
 */
export function useDocumentThread({
  documentId,
  agentId,
  reloadDocument,
  refreshPendingEdits,
}: DocumentThreadDeps) {
  const onRunSettled = useCallback(async () => {
    // Order matters: the pending list is what the inline review renders, and a
    // decided edit has to stop being offered before the document it changed is
    // re-read, or the review flashes back over the text it already applied.
    await refreshPendingEdits().catch(() => undefined);
    await reloadDocument().catch(() => undefined);
  }, [refreshPendingEdits, reloadDocument]);

  return useSubjectThread({
    kind: "document",
    subjectId: documentId,
    agentId,
    onRunSettled,
    // The parked edit is the whole point of this panel. Without this refresh
    // the hunks stay unreviewable until something else happens to refetch.
    onToolProposed: refreshPendingEdits,
  });
}
