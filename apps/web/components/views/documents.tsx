"use client";

import { FileText, History, Plus, RotateCcw, Save, Trash2 } from "lucide-react";
import type {
  DocumentKind,
  DocumentSummary,
  DocumentVersion,
  Folder,
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
import { FileTree, type FolderOps } from "./file-tree";
import { folderPath } from "./folder-tree";
import { DOCUMENT_KIND_LABELS } from "./shared";

export type DocumentsViewProps = {
  documents: DocumentSummary[];
  folders: Folder[];
  folderOps: FolderOps;
  active: WorkspaceDocument | null;
  versions: DocumentVersion[];
  openDocument: (documentId: string) => Promise<void>;
  createDocument: (title: string, kind: DocumentKind, folderId: string) => Promise<void>;
  saveDocument: (documentId: string, content: string) => Promise<void>;
  restoreVersion: (documentId: string, versionId: string) => Promise<void>;
  removeDocument: (document: DocumentSummary) => Promise<void>;
  /** Agent writes awaiting approval; optional until the workspace wires them. */
  pendingEdits?: PendingDocumentEdit[];
  decidePendingEdit?: PendingDecision;
};

/**
 * Markdown plus TeX maths, rendered with KaTeX: `$…$` and `$$…$$` both work.
 *
 * Not a TeX compilation target — \documentclass and friends are shown verbatim,
 * because nothing here produces a PDF. A user who wants one wants
 * Create → LaTeX document, which makes a LaTeX *project*.
 */
function MathMarkdown({ content }: { content: string }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkMath]} rehypePlugins={[rehypeKatex]}>
      {content}
    </ReactMarkdown>
  );
}

/**
 * The preview pane, which is a different promise per kind.
 *
 * "text" means *text*: no headings, no emphasis, no maths, no smart quotes —
 * what you typed, in a monospace pane. Running it through ReactMarkdown "just
 * in case" would be the same category of lie the LaTeX kind used to tell, where
 * the format's name and the format's behaviour were two different things.
 */
export function DocumentBody({
  kind,
  content,
}: {
  kind: DocumentKind;
  content: string;
}) {
  if (kind === "text") {
    return <pre className="document-plain">{content}</pre>;
  }
  return <MathMarkdown content={content} />;
}

export function DocumentsView({
  documents,
  folders,
  folderOps,
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
  // Which folder a new file lands in. Set by "New file here" in the tree, so
  // the answer is whatever the user was pointing at rather than always the top.
  const [newFolder, setNewFolder] = useState("");

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
          <span>Files</span>
          <button
            className="icon-button"
            onClick={() => {
              setNewFolder("");
              setCreating((value) => !value);
            }}
            aria-label="New document"
          >
            <Plus size={16} />
          </button>
        </div>
        {creating && (
          <form
            className="documents-new"
            aria-label="New document"
            onSubmit={async (event) => {
              event.preventDefault();
              if (!newTitle.trim()) return;
              await createDocument(newTitle.trim(), newKind, newFolder);
              setNewTitle("");
              setNewFolder("");
              setCreating(false);
            }}
          >
            {/* Where it will land, said out loud. Without this the only tell
                that "New file here" chose a folder is the row it appears on
                after the fact. */}
            <span className="field-hint">
              In {newFolder ? folderPath(folders, newFolder) : "Top level"}
            </span>
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
              {Object.entries(DOCUMENT_KIND_LABELS).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
            <button type="submit" className="primary-button">
              Create
            </button>
          </form>
        )}
        <FileTree
          folders={folders}
          documents={documents}
          activeId={active?.id ?? ""}
          openDocument={openDocument}
          ops={folderOps}
          onNewDocument={(folderId) => {
            setNewFolder(folderId);
            setCreating(true);
          }}
        />
      </aside>

      {active ? (
        <section className="document-editor">
          <header className="document-head">
            <div>
              <h2>{active.title}</h2>
              <span className="doc-kind">{DOCUMENT_KIND_LABELS[active.kind]}</span>
              {/* Where this file lives, on the file itself. A tree can be
                  scrolled away from the row you opened, and "which folder am I
                  editing out of" is then unanswerable without hunting. */}
              <span className="doc-kind">
                {active.folder_id ? folderPath(folders, active.folder_id) : "Top level"}
              </span>
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
                    folder_id: active.folder_id,
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
              <DocumentBody kind={active.kind} content={draft} />
            </div>
          </div>
        </section>
      ) : (
        <section className={approvals ? "document-editor" : "document-editor empty"}>
          {approvals ?? (
            <div className="empty-state">
              <FileText size={22} />
              <p>Select a document.</p>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
