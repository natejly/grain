"use client";

import { FileText, History, Plus, RotateCcw, Save, Trash2 } from "lucide-react";
import type {
  DocumentKind,
  DocumentSummary,
  DocumentVersion,
  WorkspaceDocument,
} from "@workspace/api-client";
import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkMath from "remark-math";
import {
  PendingEditList,
  type PendingDecision,
  type PendingDocumentEdit,
} from "./document-pending";

export type DocumentsViewProps = {
  documents: DocumentSummary[];
  active: WorkspaceDocument | null;
  versions: DocumentVersion[];
  openDocument: (documentId: string) => Promise<void>;
  createDocument: (title: string, kind: DocumentKind) => Promise<void>;
  saveDocument: (documentId: string, content: string) => Promise<void>;
  restoreVersion: (documentId: string, versionId: string) => Promise<void>;
  removeDocument: (document: DocumentSummary) => Promise<void>;
  /** Agent writes awaiting approval; optional until the workspace wires them. */
  pendingEdits?: PendingDocumentEdit[];
  decidePendingEdit?: PendingDecision;
};

/**
 * Markdown plus LaTeX math, rendered with KaTeX. `$…$` and `$$…$$` work in both
 * document kinds; a "latex" document is prose with math, not a TeX compilation
 * target — \documentclass and friends are shown verbatim.
 */
export function MathMarkdown({ content }: { content: string }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkMath]} rehypePlugins={[rehypeKatex]}>
      {content}
    </ReactMarkdown>
  );
}

export function DocumentsView({
  documents,
  active,
  versions,
  openDocument,
  createDocument,
  saveDocument,
  restoreVersion,
  removeDocument,
  pendingEdits,
  decidePendingEdit,
}: DocumentsViewProps) {
  const [draft, setDraft] = useState("");
  const [dirty, setDirty] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [newKind, setNewKind] = useState<DocumentKind>("markdown");

  // The agent edits documents underneath us, so re-sync whenever the loaded
  // document changes identity or content — unless the user has unsaved work.
  useEffect(() => {
    if (!active) {
      setDraft("");
      setDirty(false);
      return;
    }
    setDraft(active.content);
    setDirty(false);
  }, [active?.id, active?.updated_at]); // eslint-disable-line react-hooks/exhaustive-deps

  // A proposed create has no document to sit under, so it rides along with the
  // open document's own proposals rather than hiding until the doc exists.
  const pending = pendingEdits ?? [];
  const proposals = [
    ...(active ? pending.filter((edit) => edit.document_id === active.id) : []),
    ...pending.filter((edit) => edit.name === "create_document"),
  ];
  const approvals =
    decidePendingEdit && proposals.length > 0 ? (
      <PendingEditList edits={proposals} decide={decidePendingEdit} />
    ) : null;

  return (
    <div className="documents-layout">
      <aside className="documents-list">
        <div className="documents-list-head">
          <span>Documents</span>
          <button
            className="icon-button"
            onClick={() => setCreating((value) => !value)}
            aria-label="New document"
          >
            <Plus size={16} />
          </button>
        </div>
        {creating && (
          <form
            className="documents-new"
            onSubmit={async (event) => {
              event.preventDefault();
              if (!newTitle.trim()) return;
              await createDocument(newTitle.trim(), newKind);
              setNewTitle("");
              setCreating(false);
            }}
          >
            <input
              value={newTitle}
              onChange={(event) => setNewTitle(event.target.value)}
              placeholder="Title"
              autoFocus
            />
            <select
              value={newKind}
              onChange={(event) => setNewKind(event.target.value as DocumentKind)}
            >
              <option value="markdown">Markdown</option>
              <option value="latex">LaTeX</option>
            </select>
            <button type="submit" className="primary-button">
              Create
            </button>
          </form>
        )}
        {documents.length === 0 ? (
          <p className="documents-empty">
            No documents yet. Create one, or ask the assistant to draft it.
          </p>
        ) : (
          <ul>
            {documents.map((item) => (
              <li key={item.id}>
                <button
                  className={active?.id === item.id ? "doc-item active" : "doc-item"}
                  onClick={() => void openDocument(item.id)}
                >
                  <FileText size={14} />
                  <span className="doc-title">{item.title}</span>
                  <span className="doc-kind">{item.kind}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </aside>

      {active ? (
        <section className="document-editor">
          <header className="document-head">
            <div>
              <h2>{active.title}</h2>
              <span className="doc-kind">{active.kind}</span>
            </div>
            <div className="document-actions">
              <button
                className="ghost-button"
                onClick={() => setShowHistory((value) => !value)}
              >
                <History size={14} /> History
              </button>
              <button
                className="primary-button"
                disabled={!dirty}
                onClick={async () => {
                  await saveDocument(active.id, draft);
                  setDirty(false);
                }}
              >
                <Save size={14} /> {dirty ? "Save" : "Saved"}
              </button>
              <button
                className="icon-button"
                onClick={() =>
                  void removeDocument({
                    id: active.id,
                    title: active.title,
                    kind: active.kind,
                    characters: active.content.length,
                    updated_at: active.updated_at,
                  })
                }
                aria-label="Delete document"
              >
                <Trash2 size={15} />
              </button>
            </div>
          </header>

          {showHistory && (
            <div className="document-history">
              {versions.length === 0 ? (
                <p>No earlier versions yet.</p>
              ) : (
                <ul>
                  {versions.map((version) => (
                    <li key={version.id}>
                      <span>{version.summary}</span>
                      <button
                        className="ghost-button"
                        onClick={() => void restoreVersion(active.id, version.id)}
                      >
                        <RotateCcw size={13} /> Restore
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          {approvals}

          <div className="document-panes">
            <textarea
              className="document-source"
              value={draft}
              spellCheck
              onChange={(event) => {
                setDraft(event.target.value);
                setDirty(true);
              }}
            />
            <div className="document-preview">
              <MathMarkdown content={draft} />
            </div>
          </div>
        </section>
      ) : (
        <section className={approvals ? "document-editor" : "document-editor empty"}>
          {approvals ?? (
            <div className="empty-state">
              <FileText size={22} />
              <p>Select a document, or ask the assistant to write one.</p>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
