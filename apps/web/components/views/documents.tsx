"use client";

import {
  FileText,
  History,
  MessageSquare,
  Plus,
  RotateCcw,
  Save,
  Trash2,
} from "lucide-react";
import type {
  Citation,
  DocumentKind,
  DocumentSummary,
  DocumentVersion,
  Folder,
  GeneratedApp,
  Source,
  WorkspaceDocument,
} from "@workspace/api-client";
import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkMath from "remark-math";
import { useDocumentThread } from "../use-document-thread";
import {
  PendingEditList,
  type PendingDecision,
  type PendingDocumentEdit,
} from "./document-pending";
import { DocumentReview, type HunkDecision } from "./document-review";
import { FileTree, type FolderOps } from "./file-tree";
import { folderPath } from "./folder-tree";
import { DOCUMENT_KIND_LABELS } from "./shared";
import { SubjectChatPanel } from "./subject-chat";

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
  decidePendingEdit?: HunkDecision & PendingDecision;
  /** What the side chat needs to be the same chat as the rail's. */
  chat?: DocumentChatDeps;
};

/**
 * Everything the panel beside the document needs from the shell. Optional as a
 * bundle rather than field by field, so a caller either wires the panel or does
 * not have one — there is no half-wired state where the composer sends into
 * nothing.
 */
export type DocumentChatDeps = {
  agentId?: string;
  sources: Source[];
  apps: GeneratedApp[];
  /** The shell's provenance drawer, which renders above this panel. */
  openCitation: (citation: Citation) => Promise<void>;
  reloadDocument: () => Promise<void>;
  refreshPendingEdits: () => Promise<void>;
  /** `DEV_UNRESTRICTED_AGENT` is on, so the panel wears the warning. */
  unrestricted?: boolean;
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
  chat,
}: DocumentsViewProps) {
  const [draft, setDraft] = useState("");
  const [dirty, setDirty] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [showChat, setShowChat] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [newKind, setNewKind] = useState<DocumentKind>("markdown");
  // Which folder a new file lands in. Set by "New file here" in the tree, so
  // the answer is whatever the user was pointing at rather than always the top.
  const [newFolder, setNewFolder] = useState("");

  /**
   * The thread lives here rather than inside the panel so that closing the
   * panel does not abandon a run. Unmounting the panel would take its event
   * stream with it: the turn would keep going on the server, the write would
   * still park, and the user would have no card to answer it with.
   *
   * Empty document id when the panel is not wired, which is what keeps this
   * from creating a thread for a caller that has no panel to show it in.
   */
  const thread = useDocumentThread({
    documentId: chat && active ? active.id : "",
    agentId: chat?.agentId,
    reloadDocument: chat?.reloadDocument ?? (async () => undefined),
    refreshPendingEdits: chat?.refreshPendingEdits ?? (async () => undefined),
  });

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
  /**
   * The one proposal the inline reviewer owns: an edit to the open document
   * that the server broke into hunks. Everything else — a create, a target that
   * no longer resolves, a `find` that has gone stale, a document too long to
   * ship line by line — arrives with no segments and keeps the all-or-nothing
   * card, which is the honest offer when there is nothing to review piecewise.
   */
  const reviewable = decidePendingEdit
    ? proposals.find(
        (edit) =>
          edit.name === "edit_document" &&
          edit.document_id === active?.id &&
          edit.segments.length > 0,
      )
    : undefined;
  const carded = proposals.filter((edit) => edit.id !== reviewable?.id);
  /** Every proposal the editor column already offers a decision for. Empty when
      it offers none, so the panel is never the *only* place to answer one. */
  const decided = new Set(
    decidePendingEdit ? proposals.map((edit) => edit.id) : [],
  );
  const approvals =
    decidePendingEdit && carded.length > 0 ? (
      <PendingEditList edits={carded} decide={decidePendingEdit} />
    ) : null;

  return (
    <div className={showChat && chat ? "documents-layout with-chat" : "documents-layout"}>
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
              aria-label="Title"
              autoFocus
            />
            <select
              value={newKind}
              aria-label="Format"
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
              {chat && (
                <button
                  className="ghost-button"
                  aria-pressed={showChat}
                  onClick={() => setShowChat((value) => !value)}
                >
                  <MessageSquare size={14} /> Chat
                </button>
              )}
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

          {/* The reviewer replaces the editor rather than sitting above it. The
              document is mid-proposal: an editable textarea beside it would let
              a user type into text the agent is asking to change, and whichever
              of the two writes last would silently win. */}
          {reviewable && decidePendingEdit ? (
            /* Keyed on the proposal, so a second one arriving while the first
               is still on screen gets a fresh reviewer. Without it React reuses
               the instance and its staged rejections, and the new diff opens
               with hunks already crossed out that nobody crossed out — with the
               Apply count agreeing. */
            <DocumentReview
              key={reviewable.id}
              edit={reviewable}
              decide={decidePendingEdit}
            />
          ) : (
            <div className="document-panes">
              <textarea
                className="document-source"
                value={draft}
                spellCheck
                aria-label="Document source"
                onChange={(event) => {
                  setDraft(event.target.value);
                  setDirty(true);
                }}
              />
              <div className="document-preview">
                <DocumentBody kind={active.kind} content={draft} />
              </div>
            </div>
          )}
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

      {showChat && chat && active && (
        <SubjectChatPanel
          className="document-chat"
          heading="Chat about this document"
          label={`Chat about ${active.title}`}
          close={() => setShowChat(false)}
          thread={thread}
          sources={chat.sources}
          apps={chat.apps}
          openCitation={chat.openCitation}
          // Only what the editor column is not already holding — the inline
          // reviewer *and* every all-or-nothing card beside it.
          hidden={decided}
          unrestricted={chat.unrestricted}
        />
      )}
    </div>
  );
}
