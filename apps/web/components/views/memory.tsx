"use client";

import { Search, Trash2 } from "lucide-react";
import type { MemoryItem } from "@workspace/api-client";
import { useState } from "react";
import { formatRelative } from "./shared";

export type MemoryViewProps = {
  memories: MemoryItem[];
  forgetMemory: (item: MemoryItem) => Promise<void>;
};

/** Everything a memory can be matched on, lowercased once per row per query. */
function haystack(item: MemoryItem): string {
  return [item.content, item.kind.replaceAll("_", " "), ...item.entity_names]
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
