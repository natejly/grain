"use client";

import {
  FileText,
  GitPullRequestArrow,
  History,
  Link2,
  MessageSquare,
  MessageSquareText,
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
import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkMath from "remark-math";
import { PaneToggle, useCollapsiblePane } from "../collapsible-pane";
import { LiveCursorLayer } from "../live-cursors";
import { ShareLinksModal } from "../share-links-modal";
import type { CoworkingState } from "../use-coworking";
import { FavoriteStar, type FavoritesApi } from "./favorites";
import { useDocumentThread } from "../use-document-thread";
import {
  isEditing,
  LiveEditBanner,
  liveDraftOf,
  RemoteCaretLayer,
} from "./document-live";
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
  /** Open the shell's comments drawer about this document. */
  openComments?: (document: WorkspaceDocument) => void;
  /** Agent writes awaiting approval; optional until the workspace wires them. */
  pendingEdits?: PendingDocumentEdit[];
  decidePendingEdit?: HunkDecision & PendingDecision;
  /** The shell's one favorites list; optional so a caller without the sidebar
      block (tests, embeds) simply has no star. */
  favorites?: FavoritesApi;
  /** What the side chat needs to be the same chat as the rail's. */
  chat?: DocumentChatDeps;
  /**
   * The shell's live-coworking channel. Optional like `chat`: without it the
   * editor is exactly the editor it always was — no carets, no live drafts,
   * no heartbeats sent.
   */
  coworking?: CoworkingState;
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
  openComments,
  pendingEdits,
  decidePendingEdit,
  favorites,
  chat,
  coworking,
}: DocumentsViewProps) {
  const [draft, setDraft] = useState("");
  const [dirty, setDirty] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [showChat, setShowChat] = useState(false);
  // Whether the share-links modal is open, about the active document. The
  // modal is self-contained (see share-links-modal.tsx), so a boolean is all
  // the state this view holds about it.
  const [sharing, setSharing] = useState(false);
  const [creating, setCreating] = useState(false);
  const [listCollapsed, toggleList] = useCollapsiblePane("documents-list");
  const [newTitle, setNewTitle] = useState("");
  const [newKind, setNewKind] = useState<DocumentKind>("markdown");
  // The caret overlay mirrors the textarea's scroll; the ref is how the
  // textarea's onScroll reaches it without a re-render per scrolled pixel.
  const caretScrollRef = useRef<HTMLDivElement>(null);
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
  // Every document with a write parked on it, for the tree's dots — the open
  // document's proposals are on screen, but the other rows' are invisible
  // until the user happens to open them, and a queue nobody can see is a queue
  // nobody answers. Creates have no document to dot, so the empty id drops out.
  const pendingDocIds = new Set(
    pending.map((edit) => edit.document_id).filter(Boolean),
  );
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
  /**
   * Which proposal the inline reviewer currently owns the column for. Review
   * is the DEFAULT for a newly-arrived proposal — the diff has to find the
   * user without a click — but "Later" hands the column back to the editor,
   * so the state is the id under review rather than a boolean.
   *
   * Adjusted during render, not in an effect: an effect runs after paint, and
   * the editor flashing for a frame before the reviewer replaces it is the
   * silent-swap bug wearing a shorter costume. Scoped to document + proposal
   * so a new proposal reopens review and "Later" survives mere re-renders.
   */
  const [reviewing, setReviewing] = useState("");
  const [reviewScope, setReviewScope] = useState("");
  const scope = `${active?.id ?? ""}/${reviewable?.id ?? ""}`;
  if (scope !== reviewScope) {
    setReviewScope(scope);
    setReviewing(reviewable?.id ?? "");
  }
  const reviewOpen = Boolean(reviewable && decidePendingEdit) && reviewing === reviewable?.id;
  /** A parked proposal: review exists but the user pressed "Later". The editor
      is back on screen, read-only — the swap always prevented a concurrent
      edit, and the banner state must not silently allow a race the server
      would reject. */
  const reviewParked = Boolean(reviewable && decidePendingEdit) && !reviewOpen;
  /** The interlock the banner promises, applied to EVERY write path. A pending
      proposal pauses editing whether review is open or parked — a stale draft
      saved (button, ⌘S, or a history Restore) would change the document under
      the proposal and stale its hunks, which is exactly the concurrent-write
      race the pause exists to prevent. */
  const editingPaused = Boolean(reviewable && decidePendingEdit);

  // --- Live coworking over this document -----------------------------------
  // `report`/`leave` are the hook's stable callbacks, deliberately extracted:
  // the `coworking` object changes identity on every presence frame, and
  // depending on it would tear presence down per frame.
  const surface = active && coworking ? `document:${active.id}` : "";
  const report = coworking?.report;
  const leaveSurface = coworking?.leave;
  const typingTimer = useRef<number | null>(null);

  // Arrive when a document opens, and say goodbye when it closes — the chips
  // and carets elsewhere clear in one tick instead of a TTL.
  useEffect(() => {
    if (!surface || !report || !leaveSurface) return;
    report(surface, { typing: false });
    return () => leaveSurface(surface);
  }, [surface, report, leaveSurface]);

  // A save (dirty going false) retires the live draft: what was streaming as
  // "unsaved reality" is now simply the document.
  useEffect(() => {
    if (!surface || !report || dirty) return;
    report(surface, { typing: false });
  }, [dirty, surface, report]);

  /**
   * The heartbeat a keystroke or a caret move sends: position, selection,
   * whether keys are landing, and — while typing — the draft itself, which is
   * what a follower renders to watch the text arrive. The idle timer drops
   * `typing` (keeping the draft) so a pause reads as a pause.
   */
  function reportEditing(
    element: HTMLTextAreaElement,
    typing: boolean,
    text: string,
  ) {
    if (!surface || !report) return;
    const state = {
      cursor: element.selectionStart,
      selection_start: element.selectionStart,
      selection_end: element.selectionEnd,
      typing,
      ...(typing || dirty ? { draft: text } : {}),
    };
    report(surface, state);
    if (typingTimer.current !== null) window.clearTimeout(typingTimer.current);
    if (typing) {
      typingTimer.current = window.setTimeout(() => {
        report(surface, { ...state, typing: false });
      }, 2_500);
    }
  }

  const others = surface && coworking ? coworking.othersOn(surface) : [];
  const liveEditor = liveDraftOf(others);
  const editingOther = others.find(isEditing) ?? null;
  /**
   * Following: they type, you watch — their draft fills the panes live, and
   * your first keystroke takes a private copy of it to edit. Only while this
   * pane has no unsaved work of its own; two dirty drafts is the clash
   * banner's business, not a silent swap's.
   */
  const following = Boolean(liveEditor) && !dirty && !editingPaused;
  const clash = Boolean(editingOther) && dirty;
  const shownText =
    following && liveEditor ? String(liveEditor.state.draft) : draft;

  // Cmd/Ctrl+S saves the open document — the shortcut every editor teaches,
  // caught at the document level so it works from the textarea and from the
  // preview alike, and always prevented so the browser's own save dialog never
  // appears over an editor that has its own Save. No-op when nothing changed —
  // and refused outright while a proposal is pending, same as the Save button:
  // a shortcut must not slip through the interlock the button honors. Declared
  // below the pause computation because it reads it.
  useEffect(() => {
    if (!active) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "s" || !(event.metaKey || event.ctrlKey)) return;
      event.preventDefault();
      if (!dirty || editingPaused) return;
      void saveDocument(active.id, draft).then(() => setDirty(false));
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [active, dirty, draft, saveDocument, editingPaused]);
  // The honest count: hunks the reviewer can decide, not context stretches.
  const proposedHunks = reviewable
    ? reviewable.segments.filter((segment) => segment.index >= 0).length
    : 0;
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
    <div
      className={[
        "documents-layout",
        showChat && chat ? "with-chat" : "",
        listCollapsed ? "list-collapsed" : "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <aside
        id="documents-file-list"
        className={listCollapsed ? "documents-list collapsed" : "documents-list"}
      >
        {/* Inside the pane, first, and the one thing left showing when it is
            collapsed — the pane shrinks to a strip around this button rather
            than vanishing, because there is no header out here that survives
            the empty state to put it in. */}
        <div className="documents-list-head">
          <PaneToggle
            subject="file list"
            collapsed={listCollapsed}
            toggle={toggleList}
            controls="documents-file-list"
          />
          <span>Documents</span>
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
          pendingIds={pendingDocIds}
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
              {openComments && (
                <button
                  className="ghost-button"
                  onClick={() => openComments(active)}
                >
                  <MessageSquareText size={14} /> Comments
                </button>
              )}
              <button
                className="ghost-button"
                onClick={() => setSharing(true)}
              >
                <Link2 size={14} /> Share
              </button>
              <button
                className="ghost-button"
                onClick={() => setShowHistory((value) => !value)}
              >
                <History size={14} /> History
              </button>
              {/* Beside Save because that is where "this document, as a
                  thing" lives; the sidebar's Favorites block is what it
                  feeds. */}
              {favorites && (
                <FavoriteStar
                  kind="document"
                  targetId={active.id}
                  label={active.title}
                  favorites={favorites}
                />
              )}
              <button
                className="primary-button"
                disabled={!dirty || editingPaused}
                title={
                  editingPaused
                    ? "Editing is paused while proposed changes are pending"
                    : undefined
                }
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
                        disabled={editingPaused}
                        title={
                          editingPaused
                            ? "Editing is paused while proposed changes are pending"
                            : undefined
                        }
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

          {/* The proposal is parked, not gone: the banner names it, offers the
              way back in, and explains why the editor underneath is read-only. */}
          {reviewable && reviewParked && (
            <div className="document-review-banner" role="status">
              <GitPullRequestArrow size={15} />
              <div>
                <strong>
                  {proposedHunks > 0
                    ? `The agent proposed ${proposedHunks} change${proposedHunks === 1 ? "" : "s"}`
                    : "The agent proposed changes"}
                </strong>
                <span className="field-hint">
                  Editing is paused while changes are pending so the two of you
                  don’t overwrite each other.
                </span>
              </div>
              <button
                type="button"
                className="primary-button"
                onClick={() => setReviewing(reviewable.id)}
              >
                Review
              </button>
            </div>
          )}

          {/* The reviewer replaces the editor rather than sitting above it. The
              document is mid-proposal: an editable textarea beside it would let
              a user type into text the agent is asking to change, and whichever
              of the two writes last would silently win. */}
          {reviewable && decidePendingEdit && reviewOpen ? (
            <>
              {/* The exit, beside the reviewer rather than in it: DocumentReview
                  stays a pure decide-the-diff component, and "Later" is the
                  column's business — it parks the proposal and hands the space
                  back to the editor, with the banner above holding the way in. */}
              <div className="document-review-exit">
                <span className="field-hint">
                  Not ready to decide? The proposal keeps until you are.
                </span>
                <button
                  type="button"
                  className="ghost-button"
                  onClick={() => setReviewing("")}
                >
                  Later
                </button>
              </div>
              {/* Keyed on the proposal, so a second one arriving while the first
                  is still on screen gets a fresh reviewer. Without it React
                  reuses the instance and its staged rejections, and the new diff
                  opens with hunks already crossed out that nobody crossed out —
                  with the Apply count agreeing. */}
              <DocumentReview
                key={reviewable.id}
                edit={reviewable}
                decide={decidePendingEdit}
              />
            </>
          ) : (
            <>
              {(following || clash) && editingOther && (
                <LiveEditBanner
                  editor={liveEditor ?? editingOther}
                  following={following}
                />
              )}
              {/* The panes, not the whole editor: the chrome above them
                  (title, actions, history) is the same on both screens, so a
                  pointer over it points at nothing shared. Inside here, both
                  people are looking at the same text. */}
              <LiveCursorLayer
                surface={surface}
                coworking={coworking ?? undefined}
                className="document-panes"
              >
                <div className="document-source-wrap">
                  <textarea
                    className={
                      following ? "document-source following" : "document-source"
                    }
                    value={shownText}
                    spellCheck
                    readOnly={reviewParked}
                    aria-label="Document source"
                    onChange={(event) => {
                      // While following, this first keystroke is the takeover:
                      // the followed draft becomes a private copy, edited from
                      // the change the keystroke just made to it.
                      setDraft(event.target.value);
                      setDirty(true);
                      reportEditing(event.target, true, event.target.value);
                    }}
                    onSelect={(event) => {
                      if (!dirty) return;
                      reportEditing(event.currentTarget, false, shownText);
                    }}
                    onScroll={(event) => {
                      if (caretScrollRef.current) {
                        caretScrollRef.current.scrollTop =
                          event.currentTarget.scrollTop;
                        caretScrollRef.current.scrollLeft =
                          event.currentTarget.scrollLeft;
                      }
                    }}
                  />
                  <RemoteCaretLayer
                    text={shownText}
                    others={others}
                    scrollRef={caretScrollRef}
                  />
                </div>
                <div className="document-preview">
                  <DocumentBody kind={active.kind} content={shownText} />
                </div>
              </LiveCursorLayer>
            </>
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

      {sharing && active && (
        <ShareLinksModal
          kind="document"
          resourceId={active.id}
          resourceName={active.title}
          close={() => setSharing(false)}
        />
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
