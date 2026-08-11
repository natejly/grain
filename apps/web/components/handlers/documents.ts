"use client";

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
  async function openDocument(documentId: string) {
    setError("");
    try {
      const [document, versions] = await Promise.all([
        api.getDocument(documentId),
        api.listDocumentVersions(documentId),
      ]);
      setActiveDocument(document);
      setDocumentVersions(versions);
    } catch (caught) {
      setError(describeError(caught, "Could not open that document"));
    }
  }

  async function createDocument(title: string, kind: DocumentKind) {
    setError("");
    try {
      const created = await api.createDocument(title, "", kind);
      setActiveDocument(created);
      setDocumentVersions([]);
      setDocuments(await api.listDocuments());
    } catch (caught) {
      setError(describeError(caught, "Could not create that document"));
    }
  }

  async function saveDocument(documentId: string, content: string) {
    setError("");
    try {
      setActiveDocument(await api.saveDocument(documentId, content));
      setDocuments(await api.listDocuments());
      setDocumentVersions(await api.listDocumentVersions(documentId));
    } catch (caught) {
      setError(describeError(caught, "Could not save that document"));
    }
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

  /** Approve or deny an agent's document write from the Documents view. */
  async function decidePendingEdit(
    edit: PendingDocumentEdit,
    decision: "approved" | "denied",
  ) {
    setError("");
    // Same endpoint the chat card uses, so the parked run resumes identically.
    // A failure propagates rather than being reported here: the card disables
    // both buttons for the duration and re-enables them only in its own catch,
    // so swallowing the rejection left it stuck on "Applying…" with the edit
    // still outstanding. The card renders the message beside the button that
    // failed, which is where the user is looking.
    await api.decideAgentToolCall(edit.id, decision, false);
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
