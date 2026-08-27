"use client";

import type { WorkspaceDocument } from "@workspace/api-client";
import { Loader2, Save, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { describeError } from "./shared";

/**
 * One attached file, open for editing beside the chat that is about it.
 *
 * Deliberately a small editor and not the Documents view. The Documents pane is
 * a whole workspace surface — folders, versions, proposal review, favourites,
 * live co-editing — and reaching for it here would put a second navigation tree
 * inside a column the width of a chat. What this pane owes the user is the
 * thing they asked for: see the file the conversation is about, fix a line,
 * save. Everything else about the document is one click away on its own page,
 * and both write through the same `PUT /api/documents/{id}`.
 *
 * The load is by id on mount rather than handed down as a prop: the chip knows
 * a document id, and the content may have changed since the thread opened —
 * the agent edits these files too, which is most of the point.
 */
export function AttachmentPane({
  documentId,
  filename,
  onClose,
}: {
  documentId: string;
  filename: string;
  onClose: () => void;
}) {
  const [document, setDocument] = useState<WorkspaceDocument | null>(null);
  const [draft, setDraft] = useState("");
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  // Guards the load against a pane closed (or re-pointed) mid-flight, so a
  // resolved fetch never writes over a draft belonging to another file.
  const wanted = useRef(documentId);

  useEffect(() => {
    wanted.current = documentId;
    setDocument(null);
    setDirty(false);
    setError("");
    void api
      .getDocument(documentId)
      .then((loaded) => {
        if (wanted.current !== documentId) return;
        setDocument(loaded);
        setDraft(loaded.content);
      })
      .catch((caught) => {
        if (wanted.current !== documentId) return;
        setError(describeError(caught, "Could not open that file"));
      });
  }, [documentId]);

  const save = useCallback(async () => {
    if (!dirty || saving) return;
    setSaving(true);
    setError("");
    try {
      const saved = await api.saveDocument(documentId, draft);
      setDocument(saved);
      setDirty(false);
    } catch (caught) {
      setError(describeError(caught, "Could not save that file"));
    } finally {
      setSaving(false);
    }
  }, [documentId, draft, dirty, saving]);

  // Cmd/Ctrl+S, the shortcut every editor teaches — and always prevented, so
  // the browser's own save dialog never appears over a pane that has its own
  // Save. Scoped to this pane's subtree so two open panes do not both answer.
  const rootRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const node = rootRef.current;
    if (!node) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "s" || !(event.metaKey || event.ctrlKey)) return;
      event.preventDefault();
      void save();
    };
    node.addEventListener("keydown", onKeyDown);
    return () => node.removeEventListener("keydown", onKeyDown);
  }, [save]);

  return (
    <div className="attachment-pane" ref={rootRef}>
      <header className="attachment-pane-head">
        <span className="attachment-pane-title" title={filename}>
          {filename}
        </span>
        <button
          className="primary-button"
          disabled={!dirty || saving}
          onClick={() => void save()}
        >
          {saving ? <Loader2 size={14} className="spin" /> : <Save size={14} />}{" "}
          {dirty ? "Save" : "Saved"}
        </button>
        <button className="icon-button" onClick={onClose} aria-label="Close file">
          <X size={14} />
        </button>
      </header>
      {error && (
        <p className="attachment-pane-error" role="alert">
          {error}
        </p>
      )}
      {document === null ? (
        // No editor until the file is actually here. An empty textarea over a
        // document that failed to load invites the user to "fix" the blank and
        // save it, which would destroy the very file they came to edit — so
        // the error stands alone and there is nothing to type into.
        !error && <p className="attachment-pane-loading">Opening…</p>
      ) : (
        <textarea
          className="attachment-pane-source"
          value={draft}
          spellCheck
          aria-label={`Contents of ${filename}`}
          onChange={(event) => {
            setDraft(event.target.value);
            setDirty(true);
          }}
        />
      )}
    </div>
  );
}
