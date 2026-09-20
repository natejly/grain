"use client";

import { Pencil, Plus, Search, Trash2, User, Users } from "lucide-react";
import type {
  Conversation,
  GraphEntity,
  KnowledgeGraph,
  MemoryItem,
  Space,
} from "@workspace/api-client";
import { Fragment, useState } from "react";
import { formatRelative } from "./shared";
import { spaceNameForId } from "./space-threads";
import { useFocusReveal } from "./use-focus-reveal";

export type MemoryViewProps = {
  memories: MemoryItem[];
  forgetMemory: (item: MemoryItem) => Promise<void>;
  // The cross-link surface, optional so the view still stands alone (tests
  // mount it bare): conversations resolve where a memory was learned, the
  // graph resolves its entity names, and the two open* callbacks are the ways
  // out — to the thread that taught it and to its projection in the graph.
  conversations?: Conversation[];
  spaces?: Space[];
  graph?: KnowledgeGraph | null;
  openConversation?: (conversationId: string) => void;
  openEntity?: (entityId: string) => void;
  focused?: string | null;
  setFocused?: (id: string | null) => void;
  /**
   * Add a memory by hand. Optional like the cross-links, so the view still
   * mounts bare; no space picker is offered on purpose — a manual add lands
   * on the workspace-wide shelf, and scope stays a choice between exactly
   * "mine" and "everyone's".
   */
  addMemory?: (input: {
    content: string;
    kind: "fact" | "preference";
    shared: boolean;
  }) => Promise<boolean | void>;
  /** Rewrite one memory's sentence in place — a new value, never a re-scope.
   *  Summary rows never offer it: the rolling summary rewrites itself.
   *  Both handlers resolve `false` when the server refused the write (the
   *  409 dedup, a network blip); the form then stays open with the draft.
   *  `void` — older stubs, read-only mounts — counts as accepted. */
  editMemory?: (item: MemoryItem, content: string) => Promise<boolean | void>;
};

/**
 * Everything a memory can be matched on, lowercased once per row per query.
 * Scope joins it so "personal" and "shared" are searchable words — the list
 * holds both kinds now, and filtering to one of them is the obvious thing to
 * want once you notice they differ. The space name rides along with the word
 * "space" for the same reason: the chip makes scoping visible, so "research
 * space" has to find what the eye already can.
 */
function haystack(item: MemoryItem, spaceName: string): string {
  return [
    item.content,
    item.kind.replaceAll("_", " "),
    item.shared ? "shared workspace" : "personal mine",
    spaceName && `${spaceName} space`,
    ...item.entity_names,
  ]
    .join(" ")
    .toLowerCase();
}

/**
 * The graph row an entity name on a memory points at. Provenance first — the
 * entity that lists this memory among its inputs — then plain name match, for
 * a projection rebuilt since the memory was written. null renders as inert
 * text: a name the graph does not know is information, not a dead button.
 */
function entityFor(
  item: MemoryItem,
  name: string,
  entities: GraphEntity[],
): GraphEntity | null {
  const lower = name.toLowerCase();
  return (
    entities.find(
      (entity) =>
        entity.memory_ids.includes(item.id) && entity.name.toLowerCase() === lower,
    ) ??
    entities.find((entity) => entity.name.toLowerCase() === lower) ??
    null
  );
}

/**
 * What the product has learned about you, on its own page rather than as a
 * panel under the graph: you can read it, search it, and forget any of it.
 */
const noFocus = () => undefined;

export function MemoryView({
  memories,
  forgetMemory,
  conversations = [],
  spaces = [],
  graph = null,
  openConversation,
  openEntity,
  focused = null,
  setFocused = noFocus,
  addMemory,
  editMemory,
}: MemoryViewProps) {
  const [query, setQuery] = useState("");
  // The add form and per-row edit drafts. Local component state only: an
  // abandoned half-typed memory is not a thing worth remembering, unlike a
  // composer draft, so no storage rides along.
  const [adding, setAdding] = useState(false);
  const [addDraft, setAddDraft] = useState("");
  const [addKind, setAddKind] = useState<"fact" | "preference">("fact");
  const [addShared, setAddShared] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState("");
  useFocusReveal("memory", focused, setFocused);

  const submitAdd = async () => {
    const content = addDraft.trim();
    if (!content || !addMemory) return;
    // A refused save (the dedup 409, a network blip) keeps the form open
    // with the sentence as typed — the error names why, and closing over it
    // would discard up to 900 characters the server never took.
    if ((await addMemory({ content, kind: addKind, shared: addShared })) === false) {
      return;
    }
    setAddDraft("");
    setAddKind("fact");
    setAddShared(false);
    setAdding(false);
  };

  const submitEdit = async (item: MemoryItem) => {
    const content = editDraft.trim();
    if (!content || !editMemory) return;
    // Same contract as the add form: only an accepted write closes the
    // editor; a refusal keeps the draft in place for the user to adjust.
    if ((await editMemory(item, content)) === false) return;
    setEditingId(null);
    setEditDraft("");
  };
  const entities = graph?.entities ?? [];
  const needle = query.trim().toLowerCase();
  const matches = needle
    ? memories.filter((item) =>
        haystack(item, spaceNameForId(item.space_id, spaces)).includes(needle),
      )
    : memories;

  return (
    <section className="content-page memory-page">
      <div className="page-heading">
        <div>
          <h1>Memory</h1>
          <p>What the agent learned from talking with you, rather than from your files.</p>
        </div>
        {addMemory && (
          <button
            className="ghost-button memory-add-button"
            aria-expanded={adding}
            onClick={() => setAdding((value) => !value)}
          >
            <Plus size={14} />
            Add memory
          </button>
        )}
        {memories.length > 0 && (
          <label className="memory-search">
            <Search size={14} />
            <input
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              aria-label="Search memory"
            />
          </label>
        )}
      </div>

      {addMemory && adding && (
        <div className="memory-add-form">
          <textarea
            value={addDraft}
            onChange={(event) => setAddDraft(event.target.value)}
            placeholder="Something the assistant should always know…"
            maxLength={900}
            rows={2}
            aria-label="New memory"
            autoFocus
          />
          <div className="memory-form-controls">
            <select
              value={addKind}
              onChange={(event) =>
                setAddKind(event.target.value as "fact" | "preference")
              }
              aria-label="Kind of memory"
            >
              <option value="fact">fact</option>
              <option value="preference">preference</option>
            </select>
            {/* Same title the rows' shared badge carries, so the checkbox
                says exactly what it is about to do — this is the one control
                here that widens who a sentence reaches. */}
            <label
              className="memory-form-shared"
              title="Every member of this workspace is answered from this"
            >
              <input
                type="checkbox"
                checked={addShared}
                onChange={(event) => setAddShared(event.target.checked)}
              />
              {addShared ? <Users size={12} /> : <User size={12} />}
              Share with the workspace
            </label>
            <span className="memory-form-spacer" />
            <button
              className="ghost-button"
              onClick={() => {
                setAdding(false);
                setAddDraft("");
              }}
            >
              Cancel
            </button>
            <button
              className="ghost-button"
              disabled={!addDraft.trim()}
              onClick={() => void submitAdd()}
            >
              Save
            </button>
          </div>
        </div>
      )}

      {memories.length === 0 ? (
        <div className="empty-state">
          <p>Nothing remembered yet.</p>
        </div>
      ) : (
        <div className="memory-panel">
          <div className="panel-title">
            <div>
              <strong>Remembered facts</strong>
            </div>
            <span className="panel-count">{matches.length}</span>
          </div>
          {matches.length === 0 ? (
            <div className="feature-empty">
              <Search size={20} />
              <strong>No memory matches</strong>
            </div>
          ) : (
            <div className="memory-list">
              {matches.map((item) => {
                const spaceName = spaceNameForId(item.space_id, spaces);
                const thread = item.conversation_id
                  ? conversations.find(
                      (conversation) => conversation.id === item.conversation_id,
                    )
                  : undefined;
                return (
                <div
                  className={
                    focused === item.id ? "memory-row focused" : "memory-row"
                  }
                  id={`memory-${item.id}`}
                  key={item.id}
                >
                  <div>
                    <span className={`memory-kind ${item.kind}`}>
                      {item.kind.replaceAll("_", " ")}
                    </span>
                    {/* Which of these two a memory is decides who it can reach,
                        so it belongs on the row rather than in a filter: a
                        shared fact is one every colleague is answered from. */}
                    <span
                      className={`memory-scope ${item.shared ? "shared" : "personal"}`}
                      title={
                        item.shared
                          ? "Every member of this workspace is answered from this"
                          : "Only you are answered from this"
                      }
                    >
                      {item.shared ? <Users size={10} /> : <User size={10} />}
                      {item.shared ? "shared" : "personal"}
                    </span>
                    {/* Not .memory-scope: scope says who a fact reaches, this
                        says which space's threads it informs. */}
                    {spaceName && (
                      <span
                        className="memory-space"
                        title={`Scoped to the ${spaceName} space`}
                      >
                        {spaceName}
                      </span>
                    )}
                    {editingId === item.id && editMemory ? (
                      <div className="memory-edit-area">
                        <textarea
                          value={editDraft}
                          onChange={(event) => setEditDraft(event.target.value)}
                          maxLength={900}
                          rows={2}
                          aria-label="Edit this memory"
                          autoFocus
                        />
                        <div className="memory-form-controls">
                          <span className="memory-form-spacer" />
                          <button
                            className="ghost-button"
                            onClick={() => {
                              setEditingId(null);
                              setEditDraft("");
                            }}
                          >
                            Cancel
                          </button>
                          <button
                            className="ghost-button"
                            disabled={!editDraft.trim()}
                            onClick={() => void submitEdit(item)}
                          >
                            Save
                          </button>
                        </div>
                      </div>
                    ) : (
                      <p>{item.content}</p>
                    )}
                    <small>
                      {item.entity_names.map((name) => {
                        const entity = entityFor(item, name, entities);
                        return (
                          <Fragment key={name}>
                            {entity && openEntity ? (
                              <button
                                className="knowledge-link"
                                title="Show in the graph"
                                onClick={() => openEntity(entity.id)}
                              >
                                {name}
                              </button>
                            ) : (
                              name
                            )}
                            {" · "}
                          </Fragment>
                        );
                      })}
                      {item.conversation_id && (
                        <Fragment>
                          {thread ? (
                            <button
                              className="knowledge-link"
                              title="Open the thread this was learned in"
                              onClick={() => openConversation?.(thread.id)}
                            >
                              from {thread.title || "New conversation"}
                            </button>
                          ) : (
                            // Personal thread or a deleted one — the list not
                            // holding it is the one fact either way.
                            <span className="memory-thread-gone">
                              learned in a thread you can&apos;t see or that was deleted
                            </span>
                          )}
                          {" · "}
                        </Fragment>
                      )}
                      {item.importance > 1 && `reinforced ×${item.importance} · `}
                      updated {formatRelative(item.updated_at)}
                    </small>
                  </div>
                  <div className="memory-actions">
                    {/* Never on a summary row: the rolling summary rewrites
                        itself, and the server answers a PATCH of one with a
                        422 saying exactly that. */}
                    {editMemory && item.kind !== "summary" && (
                      <button
                        className="delete-button memory-edit-button"
                        title="Edit this memory"
                        aria-label="Edit this memory"
                        onClick={() => {
                          setEditingId(item.id);
                          setEditDraft(item.content);
                        }}
                      >
                        <Pencil size={14} />
                      </button>
                    )}
                    <button
                      className="delete-button"
                      title="Forget this memory"
                      aria-label="Forget this memory"
                      onClick={() => void forgetMemory(item)}
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                </div>
                );
              })}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
