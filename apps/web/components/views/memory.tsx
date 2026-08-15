"use client";

import { Search, Trash2, User, Users } from "lucide-react";
import type { MemoryItem } from "@workspace/api-client";
import { useState } from "react";
import { formatRelative } from "./shared";

export type MemoryViewProps = {
  memories: MemoryItem[];
  forgetMemory: (item: MemoryItem) => Promise<void>;
};

/**
 * Everything a memory can be matched on, lowercased once per row per query.
 * Scope joins it so "personal" and "shared" are searchable words — the list
 * holds both kinds now, and filtering to one of them is the obvious thing to
 * want once you notice they differ.
 */
function haystack(item: MemoryItem): string {
  return [
    item.content,
    item.kind.replaceAll("_", " "),
    item.shared ? "shared workspace" : "personal mine",
    ...item.entity_names,
  ]
    .join(" ")
    .toLowerCase();
}

/**
 * What the product has learned about you, on its own page rather than as a
 * panel under the graph: you can read it, search it, and forget any of it.
 */
export function MemoryView({ memories, forgetMemory }: MemoryViewProps) {
  const [query, setQuery] = useState("");
  const needle = query.trim().toLowerCase();
  const matches = needle
    ? memories.filter((item) => haystack(item).includes(needle))
    : memories;

  return (
    <section className="content-page memory-page">
      <div className="page-heading">
        <div>
          <h1>Memory</h1>
        </div>
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
              <strong>No matches</strong>
            </div>
          ) : (
            <div className="memory-list">
              {matches.map((item) => (
                <div className="memory-row" key={item.id}>
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
                    <p>{item.content}</p>
                    <small>
                      {item.entity_names.length > 0 &&
                        `${item.entity_names.join(", ")} · `}
                      {item.importance > 1 && `reinforced ×${item.importance} · `}
                      updated {formatRelative(item.updated_at)}
                    </small>
                  </div>
                  <button
                    className="delete-button"
                    title="Forget this memory"
                    aria-label="Forget this memory"
                    onClick={() => void forgetMemory(item)}
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
