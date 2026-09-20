"use client";

import { ApiError } from "@workspace/api-client";
import type {
  DocumentKind,
  DocumentSummary,
  DocumentVersion,
  PendingDocumentEdit,
  WorkspaceDocument,
} from "@workspace/api-client";
import type { Dispatch, RefObject, SetStateAction } from "react";
import { api } from "../api";
import { describeError } from "../views/shared";

/**
 * A save the server refused because somebody else saved first: the 409's
 * machine detail, mapped for the editor's banner. `headVersionId` is the
 * base an "overwrite anyway" resend must carry to win against exactly the
 * version the user was just shown.
 */
export type SaveConflict = {
  headVersionId: string;
  updatedAt: string;
  savedBy: string;
};

/**
 * What a save attempt came to, as a value: the server acknowledged it
 * ("saved"), refused it over a newer head (the conflict, for the banner), or
 * failed some other way ("failed" — network, 500, over-length 422). Three
 * states, not two: "failed" used to be returned as the success `null`, so a
 * blown "Overwrite anyway" cleared the conflict banner and relabelled the
 * button "Saved" over a draft the server never stored.
 */
export type SaveOutcome = SaveConflict | "saved" | "failed";

export type DocumentHandlerDeps = {
  setError: Dispatch<SetStateAction<string>>;
  setDocuments: Dispatch<SetStateAction<DocumentSummary[]>>;
  setActiveDocument: Dispatch<SetStateAction<WorkspaceDocument | null>>;
  setDocumentVersions: Dispatch<SetStateAction<DocumentVersion[]>>;
  setPendingEdits: Dispatch<SetStateAction<PendingDocumentEdit[]>>;
  refreshArtifacts: () => Promise<void>;
  activeDocumentRef: RefObject<string | null>;
};

export function createDocumentHandlers({
  setError,
  setDocuments,
  setActiveDocument,
  setDocumentVersions,
  setPendingEdits,
  refreshArtifacts,
  activeDocumentRef,
}: DocumentHandlerDeps) {
  /** Returns whether the document actually loaded, so "Reload theirs" can
   *  keep its conflict banner up when the reload it promised never landed. */
  async function openDocument(documentId: string): Promise<boolean> {
    setError("");
    try {
      const [document, versions] = await Promise.all([
        api.getDocument(documentId),
        api.listDocumentVersions(documentId),
      ]);
      setActiveDocument(document);
      setDocumentVersions(versions);
      return true;
    } catch (caught) {
      setError(describeError(caught, "Could not open that document"));
      return false;
    }
  }

  async function createDocument(title: string, kind: DocumentKind, folderId = "") {
    setError("");
    try {
      const created = await api.createDocument(title, "", kind, folderId);
      setActiveDocument(created);
      setDocumentVersions([]);
      setDocuments(await api.listDocuments());
    } catch (caught) {
      setError(describeError(caught, "Could not create that document"));
    }
  }

  /**
   * Save, optionally under the precondition that `baseVersionId` is still the
   * document's head. Returns the outcome instead of throwing: this is the one
   * choke point the Save button, Cmd+S and "Overwrite anyway" all go through,
   * and a returned value keeps the view's state machine explicit — the view
   * marks the pane clean on "saved" and ONLY on "saved". A conflict
   * deliberately skips `setError` — the banner is the error surface; every
   * other failure raises the toast AND comes back as "failed" so the caller
   * never mistakes it for the write having landed.
   */
  async function saveDocument(
    documentId: string,
    content: string,
    baseVersionId?: string,
  ): Promise<SaveOutcome> {
    setError("");
    try {
      setActiveDocument(await api.saveDocument(documentId, content, baseVersionId));
    } catch (caught) {
      const detail = caught instanceof ApiError ? (caught.detail as {
        code?: string;
        head_version_id?: string;
        updated_at?: string;
        saved_by?: string;
      } | undefined) : undefined;
      if (
        caught instanceof ApiError &&
        caught.status === 409 &&
        detail?.code === "document_version_conflict"
      ) {
        return {
          headVersionId: detail.head_version_id ?? "",
          updatedAt: detail.updated_at ?? "",
          savedBy: detail.saved_by ?? "",
        };
      }
      setError(describeError(caught, "Could not save that document"));
      return "failed";
    }
    // The write landed; these listings are bookkeeping. A hiccup refreshing
    // them must not report the save as failed — the document set above
    // already carries the new head, and the view's re-sync reads it.
    await Promise.all([
      api.listDocuments().then(setDocuments),
      api.listDocumentVersions(documentId).then(setDocumentVersions),
    ]).catch(() => undefined);
    return "saved";
  }

  async function restoreDocumentVersion(documentId: string, versionId: string) {
    setError("");
    try {
      setActiveDocument(await api.restoreDocumentVersion(documentId, versionId));
      setDocumentVersions(await api.listDocumentVersions(documentId));
    } catch (caught) {
      setError(describeError(caught, "Could not restore that version"));
    }
  }

  async function removeDocument(document: DocumentSummary) {
    // The gate every other destructive action in the shell has. It matters more
    // here than for a source: a document is written in this editor and its
    // versions go with it, so there is nothing to re-upload.
    if (!window.confirm(`Delete “${document.title}” and its version history?`)) return;
    setError("");
    try {
      await api.deleteDocument(document.id);
      setActiveDocument((current) => (current?.id === document.id ? null : current));
      setDocuments(await api.listDocuments());
    } catch (caught) {
      setError(describeError(caught, "Could not delete that document"));
    }
  }

  /**
   * Block until the decided call has actually run.
   *
   * The decision endpoint acknowledges the user and resumes the run in a
   * background task, so the write lands *after* the response the caller is
   * awaiting. Refetching straight away therefore reads the pre-write document
   * and the editor keeps showing stale text until a manual reload — invisible
   * against a fast test model, guaranteed against a real one. Chat does not
   * have this problem because it is still following the run's event stream;
   * this follows the same stream, which `PendingDocumentEdit.run_id` names.
   */
  async function awaitToolCompletion(edit: PendingDocumentEdit) {
    try {
      for await (const event of api.streamRun(edit.run_id)) {
        // The stream replays from the start of the run, so match this call
        // rather than any completion: an earlier read-only tool in the same
        // turn would otherwise release the wait too early.
        if (event.event === "tool.completed" && event.data.tool_call_id === edit.id) {
          return;
        }
        // The run can end without ever reaching this call — cancelled, or
        // failed on the way. Never outlive it.
        if (["run.completed", "run.failed", "run.cancelled"].includes(event.event)) {
          return;
        }
      }
    } catch {
      // Following the run is an optimisation on refresh timing, not the
      // decision itself. A dropped stream just means a staler editor.
    }
  }

  /**
   * Approve or deny an agent's document write from the Documents view.
   *
   * `acceptedHunks` is the inline reviewer's staged selection, sent as one
   * amendment on one decision. Absent means the proposal was taken whole, which
   * is what the all-or-nothing card still sends and what a create can only mean.
   */
  async function decidePendingEdit(
    edit: PendingDocumentEdit,
    decision: "approved" | "denied",
    acceptedHunks?: number[],
  ) {
    setError("");
    // Same endpoint the chat card uses, so the parked run resumes identically.
    // A failure propagates rather than being reported here: the card disables
    // both buttons for the duration and re-enables them only in its own catch,
    // so swallowing the rejection left it stuck on "Applying…" with the edit
    // still outstanding. The card renders the message beside the button that
    // failed, which is where the user is looking.
    await api.decideAgentToolCall(edit.id, decision, false, {
      ...(acceptedHunks ? { accepted_hunks: acceptedHunks } : {}),
    });
    setPendingEdits((items) => items.filter((item) => item.id !== edit.id));
    await awaitToolCompletion(edit);
    await refreshArtifacts().catch(() => undefined);
    const open = activeDocumentRef.current;
    if (open) {
      setActiveDocument(await api.getDocument(open).catch(() => null));
      setDocumentVersions(await api.listDocumentVersions(open).catch(() => []));
    }
  }

  return {
    openDocument,
    createDocument,
    saveDocument,
    restoreDocumentVersion,
    removeDocument,
    decidePendingEdit,
  };
}
