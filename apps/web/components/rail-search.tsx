"use client";

import type { ConversationSearchHit } from "@workspace/api-client";
import { Search } from "lucide-react";
import { useEffect, useState } from "react";
import { formatRelative } from "./views/shared";

/**
 * The thread rail's persistent search: what was SAID, not only what threads
 * are named. Queries the same GET /api/conversations/search the ⌘K palette's
 * deep-search effect reads — two front doors onto one index — and its
 * behavior is copied from that effect so the doors cannot drift: 200ms
 * debounce, three characters minimum, stale replies dropped, and errors
 * swallowed to an empty list ("no index, no row — not an error state").
 *
 * Fully prop-driven — no localStorage, no shell state — so the vitest mounts
 * it bare. Keyboard follows the palette's roving-index model: focus never
 * leaves the input; ArrowUp/ArrowDown move the highlighted row, Enter with no
 * selection moves it into the results, Enter on a row opens the thread, and
 * Escape clears everything in one step.
 */

/** First hit per conversation, capped — a thread mentioned five times is one
 *  row, not five. Pure, tested without a DOM. */
export function dedupeHits(
  hits: ConversationSearchHit[],
  cap = 8,
): ConversationSearchHit[] {
  const seen = new Set<string>();
  const rows: ConversationSearchHit[] = [];
  for (const hit of hits) {
    if (seen.has(hit.conversation_id)) continue;
    seen.add(hit.conversation_id);
    rows.push(hit);
    if (rows.length >= cap) break;
  }
  return rows;
}

export function RailSearch({
  search,
  openThread,
}: {
  search: (q: string) => Promise<ConversationSearchHit[]>;
  openThread: (conversationId: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<ConversationSearchHit[]>([]);
  /** Whether the current query's reply has landed — gates the empty line, so
   *  "searching" and "nothing matches" are never the same render. */
  const [loaded, setLoaded] = useState(false);
  /** The highlighted row; -1 means the input itself. */
  const [index, setIndex] = useState(-1);

  useEffect(() => {
    setIndex(-1);
    if (query.trim().length < 3) {
      setHits([]);
      setLoaded(false);
      return;
    }
    let stale = false;
    setLoaded(false);
    const timer = window.setTimeout(() => {
      search(query.trim())
        .then((rows) => {
          if (stale) return;
          setHits(rows);
          setLoaded(true);
        })
        .catch(() => {
          // No index, no row — not an error state. But the state must still
          // SETTLE: leaving the previous query's rows on screen would present
          // another query's threads under this query's text, and leaving
          // `loaded` false would spin forever. An empty, settled list is the
          // honest answer a failed search has.
          if (stale) return;
          setHits([]);
          setLoaded(true);
        });
    }, 200);
    return () => {
      stale = true;
      window.clearTimeout(timer);
    };
    // `search` is a prop-stable dispatcher; the query is the trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query]);

  const rows = dedupeHits(hits);

  function clear() {
    setQuery("");
    setHits([]);
    setLoaded(false);
    setIndex(-1);
  }

  function open(hit: ConversationSearchHit) {
    openThread(hit.conversation_id);
    clear();
  }

  return (
    <div className="rail-search">
      <div className="rail-search-row">
        <Search size={14} aria-hidden="true" />
        <input
          value={query}
          aria-label="Search chats"
          placeholder="Search chats…"
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              clear();
              return;
            }
            if (event.key === "ArrowDown") {
              event.preventDefault();
              setIndex((value) => Math.min(value + 1, rows.length - 1));
              return;
            }
            if (event.key === "ArrowUp") {
              event.preventDefault();
              setIndex((value) => Math.max(value - 1, -1));
              return;
            }
            if (event.key === "Enter") {
              event.preventDefault();
              // First Enter moves the selection into the results; the next
              // opens the highlighted thread.
              if (index === -1) {
                if (rows.length > 0) setIndex(0);
                return;
              }
              const hit = rows[index];
              if (hit) open(hit);
            }
          }}
        />
      </div>
      {query.trim().length >= 3 && (
        <ul
          className="rail-search-results"
          role="listbox"
          aria-label="Chat search results"
        >
          {rows.map((hit, rowIndex) => (
            <li key={hit.conversation_id}>
              <button
                type="button"
                role="option"
                aria-selected={rowIndex === index}
                data-focused={rowIndex === index || undefined}
                className={
                  rowIndex === index
                    ? "rail-search-hit focused"
                    : "rail-search-hit"
                }
                onMouseEnter={() => setIndex(rowIndex)}
                onClick={() => open(hit)}
              >
                <strong>{hit.title}</strong>
                <span className="rail-search-snippet">
                  “{hit.snippet.slice(0, 80)}…”
                </span>
                <time>{formatRelative(hit.spoken_at)}</time>
              </button>
            </li>
          ))}
          {loaded && rows.length === 0 && (
            <li className="rail-search-empty">Nothing said matches.</li>
          )}
        </ul>
      )}
    </div>
  );
}
