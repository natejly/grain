"use client";

import type { Conversation, Source, Space } from "@workspace/api-client";
import { Layers, Plus, Trash2, UploadCloud } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import {
  describeError,
  formatRelative,
  groupThreads,
  statusLabel,
} from "./shared";
import { sourcesInSpace, threadsInSpace } from "./space-threads";

/**
 * A space groups rail threads under standing context: instructions the server
 * appends to every turn, knowledge files retrieved only here, and a memory
 * shelf of its own. This view manages the container; the threads themselves
 * are ordinary chat threads — clicking one goes to Chat, it does not embed a
 * transcript here.
 *
 * Mutations stay inline (the AgentsView pattern) because nothing outside this
 * view creates or edits a space; the *list* lives in use-workspace because
 * the rail chip and the thread counts read it too.
 */

type SpacesViewProps = {
  spaces: Space[];
  conversations: Conversation[];
  sources: Source[];
  setError: (message: string) => void;
  /** Re-fetches spaces and sources — refreshSecondary from the hook. */
  refreshSpaces: () => Promise<void>;
  onSelectConversation: (conversationId: string) => void;
  onNewThread: (spaceId: string) => Promise<void> | void;
};

export function SpacesView({
  spaces,
  conversations,
  sources,
  setError,
  refreshSpaces,
  onSelectConversation,
  onNewThread,
}: SpacesViewProps) {
  const [selectedId, setSelectedId] = useState("");
  const [newName, setNewName] = useState("");
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
      const space = await api.createSpace({ name: trimmed });
      setNewName("");
      setSelectedId(space.id);
      await refreshSpaces();
    } catch (caught) {
      setError(describeError(caught, "Could not create the space"));
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

  const spaceThreads = selected ? threadsInSpace(conversations, selected.id) : [];
  const grouped = groupThreads(spaceThreads);
  const spaceSources = selected ? sourcesInSpace(sources, selected.id) : [];

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
            <p>No spaces yet. A space groups threads under shared instructions and knowledge.</p>
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
                {space.thread_count} {space.thread_count === 1 ? "thread" : "threads"}
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
            <button
              className="ghost-button danger"
              onClick={() => void remove()}
              aria-label={`Delete ${selected.name}`}
            >
              <Trash2 size={14} />
              Delete space
            </button>
          </header>

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
              <strong>{uploading ? "Indexing…" : "Drop a file for this space"}</strong>
            </div>
            {spaceSources.length > 0 && (
              <ul className="space-source-list">
                {spaceSources.map((source) => (
                  <li key={source.id}>
                    <span className="space-source-name">{source.filename}</span>
                    <span className="status-pill">{statusLabel(source.status)}</span>
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

          <div className="space-threads">
            <div className="space-threads-head">
              <h2>Threads</h2>
              <button
                className="primary-button"
                onClick={() => void onNewThread(selected.id)}
              >
                <Plus size={14} />
                New thread
              </button>
            </div>
            {spaceThreads.length === 0 ? (
              <div className="empty-state">
                <p>No threads yet. A new thread here carries the space's instructions and knowledge.</p>
              </div>
            ) : (
              (["personal", "shared"] as const).map((bucket) =>
                grouped[bucket].length === 0 ? null : (
                  <div key={bucket} className="thread-group">
                    <h3>{bucket === "personal" ? "Personal" : "Shared"}</h3>
                    {grouped[bucket].map((conversation) => (
                      <button
                        key={conversation.id}
                        className="space-thread-row"
                        onClick={() => onSelectConversation(conversation.id)}
                      >
                        <span>{conversation.title}</span>
                        <time>{formatRelative(conversation.updated_at)}</time>
                      </button>
                    ))}
                  </div>
                ),
              )
            )}
          </div>
        </section>
      )}
    </div>
  );
}
