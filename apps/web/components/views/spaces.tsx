"use client";

import type {
  Conversation,
  Source,
  Space,
  SpaceTemplate,
} from "@workspace/api-client";
import {
  BookmarkPlus,
  FolderInput,
  FolderMinus,
  Layers,
  MoreHorizontal,
  Plus,
  Trash2,
  UploadCloud,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { DisclosureMenu } from "../disclosure-menu";
import {
  describeError,
  formatRelative,
  groupThreads,
  statusLabel,
} from "./shared";
import { sourcesInSpace, threadsInSpace, unspacedThreads } from "./space-threads";
import { nextTemplateName, templateBaseName } from "./template-format";

/**
 * A space is a project-shaped container: a group of chat threads under
 * standing context — instructions the server appends to every turn, knowledge
 * files retrieved only here, and a memory shelf of its own. This view IS the
 * container page: its threads can be started here, filed in from the plain
 * rail ("Add a thread"), or filed back out, and its files live beside them.
 * Clicking a thread still goes to Chat — the transcript has one home.
 *
 * Mutations stay inline (the AgentsView pattern) because nothing outside this
 * view creates or edits a space; the *list* lives in use-workspace because
 * the rail's space groups and the thread counts read it too.
 */

type SpacesViewProps = {
  spaces: Space[];
  /** Saved starting points; "New space" instantiates one when picked. */
  spaceTemplates: SpaceTemplate[];
  conversations: Conversation[];
  sources: Source[];
  setError: (message: string) => void;
  /** Re-fetches spaces, templates and sources — refreshSecondary from the hook. */
  refreshSpaces: () => Promise<void>;
  onSelectConversation: (conversationId: string) => void;
  onNewThread: (spaceId: string) => Promise<void> | void;
  /** File a thread into a space ("" removes it) — moveConversationToSpace. */
  onMoveThread: (conversationId: string, spaceId: string) => Promise<void> | void;
};

export function SpacesView({
  spaces,
  spaceTemplates,
  conversations,
  sources,
  setError,
  refreshSpaces,
  onSelectConversation,
  onNewThread,
  onMoveThread,
}: SpacesViewProps) {
  const [selectedId, setSelectedId] = useState("");
  const [newName, setNewName] = useState("");
  /** "" = a blank space; a template id = instantiate that template. */
  const [fromTemplateId, setFromTemplateId] = useState("");
  const [savingTemplate, setSavingTemplate] = useState(false);
  // The editable copies. Re-seeded whenever the selection or the stored row
  // changes, so a save elsewhere (another tab, another member) is not silently
  // overwritten by a stale buffer — same dirty-tracking shape as agents.tsx.
  const [name, setName] = useState("");
  const [instructions, setInstructions] = useState("");
  const [saving, setSaving] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const selected = spaces.find((space) => space.id === selectedId) ?? null;

  useEffect(() => {
    setName(selected?.name ?? "");
    setInstructions(selected?.instructions ?? "");
  }, [selected?.id, selected?.name, selected?.instructions]);

  const dirty =
    selected !== null &&
    (name !== selected.name || instructions !== selected.instructions);

  const create = async () => {
    const trimmed = newName.trim();
    if (!trimmed) return;
    try {
      // One form, two paths: a picked template routes the same name through
      // instantiate, so the new space starts with the saved instructions.
      const space = fromTemplateId
        ? await api.instantiateSpaceTemplate(fromTemplateId, trimmed)
        : await api.createSpace({ name: trimmed });
      setNewName("");
      setFromTemplateId("");
      setSelectedId(space.id);
      await refreshSpaces();
    } catch (caught) {
      setError(describeError(caught, "Could not create the space"));
    }
  };

  const saveAsTemplate = async () => {
    if (!selected || savingTemplate) return;
    setSavingTemplate(true);
    try {
      // The client lands on a free name so the one-click save cannot bounce
      // off the workspace's unique-name claim (see template-format.ts).
      await api.createSpaceTemplate({
        name: nextTemplateName(
          templateBaseName(selected.name),
          spaceTemplates.map((template) => template.name),
        ),
        from_space_id: selected.id,
      });
      await refreshSpaces();
    } catch (caught) {
      setError(describeError(caught, "Could not save the template"));
    } finally {
      setSavingTemplate(false);
    }
  };

  const save = async () => {
    if (!selected || !dirty) return;
    setSaving(true);
    try {
      await api.updateSpace(selected.id, {
        ...(name !== selected.name ? { name } : {}),
        ...(instructions !== selected.instructions ? { instructions } : {}),
      });
      await refreshSpaces();
    } catch (caught) {
      setError(describeError(caught, "Could not save the space"));
    } finally {
      setSaving(false);
    }
  };

  const remove = async () => {
    if (!selected) return;
    if (
      !window.confirm(
        `Delete “${selected.name}”? This deletes the space's threads, ` +
          "knowledge files, and memory.",
      )
    ) {
      return;
    }
    try {
      await api.deleteSpace(selected.id);
      setSelectedId("");
      await refreshSpaces();
    } catch (caught) {
      setError(describeError(caught, "Could not delete the space"));
    }
  };

  const uploadFiles = async (files: FileList | File[]) => {
    if (!selected) return;
    setUploading(true);
    try {
      for (const file of Array.from(files)) {
        await api.uploadSource(file, selected.id);
      }
      await refreshSpaces();
    } catch (caught) {
      setError(describeError(caught, "Could not upload the file"));
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const removeSource = async (source: Source) => {
    if (!window.confirm(`Remove “${source.filename}” from this space?`)) return;
    try {
      await api.deleteSource(source.id);
      await refreshSpaces();
    } catch (caught) {
      setError(describeError(caught, "Could not remove the file"));
    }
  };

  // Filing is not destructive in either direction — the thread keeps its
  // transcript and its own attachments; only its standing context changes —
  // so neither gesture asks for a confirm. No refresh here: the shell's
  // `moveConversationToSpace` already patches the row and re-fetches the
  // space counts, and a second refreshSpaces would just double the fetch.
  const fileThread = async (conversationId: string, spaceId: string) => {
    try {
      await onMoveThread(conversationId, spaceId);
    } catch (caught) {
      setError(describeError(caught, "Could not move the thread"));
    }
  };

  const spaceThreads = selected ? threadsInSpace(conversations, selected.id) : [];
  const grouped = groupThreads(spaceThreads);
  const spaceSources = selected ? sourcesInSpace(sources, selected.id) : [];
  // What "Add a thread" can offer: rail threads in no (known) space. Subject
  // threads never reach this list — the server keeps them out of the rail.
  const addable = unspacedThreads(spaces, conversations);

  return (
    <div className="spaces-layout">
      <aside className="spaces-list" aria-label="Spaces">
        <form
          className="spaces-create"
          onSubmit={(event) => {
            event.preventDefault();
            void create();
          }}
        >
          <input
            aria-label="Space name"
            placeholder="New space…"
            value={newName}
            onChange={(event) => setNewName(event.target.value)}
          />
          {spaceTemplates.length > 0 && (
            <select
              aria-label="From template"
              title="From template"
              value={fromTemplateId}
              onChange={(event) => setFromTemplateId(event.target.value)}
            >
              <option value="">Blank space</option>
              {spaceTemplates.map((template) => (
                <option key={template.id} value={template.id}>
                  From “{template.name}”
                </option>
              ))}
            </select>
          )}
          <button
            type="submit"
            className="primary-button"
            disabled={!newName.trim()}
          >
            <Plus size={14} />
            Create space
          </button>
        </form>
        {spaces.length === 0 ? (
          <div className="empty-state">
            <p>
              No spaces yet. A space groups threads under shared instructions
              and knowledge — like a project folder for your chats.
            </p>
          </div>
        ) : (
          spaces.map((space) => (
            <button
              key={space.id}
              className={space.id === selectedId ? "space-row active" : "space-row"}
              onClick={() => setSelectedId(space.id)}
            >
              <Layers size={14} aria-hidden />
              <span className="space-row-name">{space.name}</span>
              <span className="space-row-count">
                {space.thread_count}{" "}
                {space.thread_count === 1 ? "thread" : "threads"}
                {space.source_count > 0 &&
                  ` · ${space.source_count} ${
                    space.source_count === 1 ? "file" : "files"
                  }`}
              </span>
            </button>
          ))
        )}
      </aside>

      {selected === null ? (
        <section className="space-detail">
          <div className="empty-state">
            <p>Select a space, or create one.</p>
          </div>
        </section>
      ) : (
        <section className="space-detail" aria-label={`Space ${selected.name}`}>
          <header className="space-detail-head">
            {/* Not "Space name": the create form's input already carries that
                name, and the two render at once — an unscoped getByLabel must
                stay unambiguous. */}
            <input
              aria-label="Rename space"
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
            <button
              className="primary-button"
              onClick={() => void save()}
              disabled={!dirty || saving || !name.trim()}
            >
              {saving ? "Saving…" : "Save"}
            </button>
            {/* The rarely-used pair lives in a menu, the thread-row
                arrangement: the header keeps the name and the one everyday
                action, and the destructive one is never under a stray click. */}
            <DisclosureMenu
              id="space-actions"
              triggerLabel={`Actions for ${selected.name}`}
              triggerClassName="ghost-button space-actions-trigger"
              trigger={<MoreHorizontal size={14} />}
              menuLabel={`Actions for ${selected.name}`}
            >
              {(close) => (
                <>
                  <button
                    className="disclosure-option"
                    onClick={() => {
                      close();
                      void saveAsTemplate();
                    }}
                    disabled={savingTemplate}
                    aria-label={`Save ${selected.name} as template`}
                    title="Snapshot this space's instructions as a reusable template"
                  >
                    <BookmarkPlus size={13} />
                    {savingTemplate ? "Saving…" : "Save as template"}
                  </button>
                  <button
                    className="disclosure-option danger"
                    onClick={() => {
                      close();
                      void remove();
                    }}
                    aria-label={`Delete ${selected.name}`}
                  >
                    <Trash2 size={13} />
                    Delete space
                  </button>
                </>
              )}
            </DisclosureMenu>
          </header>

          <div className="space-detail-grid">
            <div className="space-main space-threads">
              <div className="space-threads-head">
                <h2>Threads</h2>
                <div className="space-threads-actions">
                  {addable.length > 0 && (
                    <DisclosureMenu
                      id="space-add-thread"
                      triggerLabel="Add an existing thread"
                      triggerClassName="ghost-button"
                      trigger={
                        <>
                          <FolderInput size={14} />
                          Add a thread
                        </>
                      }
                      menuLabel="Threads outside any space"
                    >
                      {(close) => (
                        <>
                          {addable.map((conversation) => (
                            <button
                              key={conversation.id}
                              className="disclosure-option"
                              aria-label={`Add ${conversation.title} to ${selected.name}`}
                              onClick={() => {
                                close();
                                void fileThread(conversation.id, selected.id);
                              }}
                            >
                              <span className="space-add-title">
                                {conversation.title}
                              </span>
                              <time>{formatRelative(conversation.updated_at)}</time>
                            </button>
                          ))}
                        </>
                      )}
                    </DisclosureMenu>
                  )}
                  <button
                    className="primary-button"
                    onClick={() => void onNewThread(selected.id)}
                  >
                    <Plus size={14} />
                    New thread
                  </button>
                </div>
              </div>
              {spaceThreads.length === 0 ? (
                <div className="empty-state">
                  <p>
                    No threads yet. Start one here, or file an existing thread
                    in — either way it carries the space&apos;s instructions
                    and knowledge.
                  </p>
                </div>
              ) : (
                (["personal", "shared"] as const).map((bucket) =>
                  grouped[bucket].length === 0 ? null : (
                    <div key={bucket} className="thread-group">
                      <h3>{bucket === "personal" ? "Personal" : "Shared"}</h3>
                      {grouped[bucket].map((conversation) => (
                        <div key={conversation.id} className="space-thread-row">
                          <button
                            className="space-thread-open"
                            onClick={() => onSelectConversation(conversation.id)}
                          >
                            <span>{conversation.title}</span>
                            <time>{formatRelative(conversation.updated_at)}</time>
                          </button>
                          <button
                            className="ghost-button"
                            aria-label={`Remove ${conversation.title} from this space`}
                            title="Back to the plain rail — the thread keeps its transcript"
                            onClick={() => void fileThread(conversation.id, "")}
                          >
                            <FolderMinus size={13} />
                          </button>
                        </div>
                      ))}
                    </div>
                  ),
                )
              )}
            </div>

            <div className="space-side">
              <label className="space-instructions">
                <span>Instructions</span>
                <textarea
                  aria-label="Space instructions"
                  placeholder="Standing instructions for every thread in this space…"
                  value={instructions}
                  onChange={(event) => setInstructions(event.target.value)}
                  rows={5}
                />
              </label>

              <div className="space-knowledge">
                <h2>Knowledge</h2>
                <div
                  className={`drop-zone ${dragging ? "dragging" : ""}`}
                  onDragEnter={(event) => {
                    event.preventDefault();
                    setDragging(true);
                  }}
                  onDragOver={(event) => event.preventDefault()}
                  onDragLeave={() => setDragging(false)}
                  onDrop={(event) => {
                    event.preventDefault();
                    setDragging(false);
                    void uploadFiles(event.dataTransfer.files);
                  }}
                  onClick={() => fileInputRef.current?.click()}
                >
                  <input
                    ref={fileInputRef}
                    type="file"
                    accept=".txt,.md,.markdown,.pdf,.csv,.json"
                    hidden
                    onChange={(event) =>
                      event.target.files && void uploadFiles(event.target.files)
                    }
                  />
                  <div className="upload-icon">
                    <UploadCloud size={18} />
                  </div>
                  <strong>
                    {uploading ? "Indexing…" : "Drop a file for this space"}
                  </strong>
                </div>
                {spaceSources.length > 0 && (
                  <ul className="space-source-list">
                    {spaceSources.map((source) => (
                      <li key={source.id}>
                        <span className="space-source-name">{source.filename}</span>
                        <span className="status-pill">
                          {statusLabel(source.status)}
                        </span>
                        <button
                          className="ghost-button"
                          aria-label={`Remove ${source.filename}`}
                          onClick={() => void removeSource(source)}
                        >
                          <Trash2 size={13} />
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          </div>
        </section>
      )}
    </div>
  );
}
