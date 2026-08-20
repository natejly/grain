"use client";

import type { Conversation, DocumentKind } from "@workspace/api-client";
import { CornerDownLeft, MessageSquare, Plus, Search } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { CreateAction } from "./views/navigation";
import { buildPaletteRows, matchPalette, type PaletteRow } from "./views/palette";
import type { View } from "./views/shared";

/**
 * ⌘K — the fifth, invisible destination.
 *
 * Everything the shell can reach, reachable by typing: every view (settings
 * surfaces included), every Create action, and every thread by title — the
 * "find that chat from Tuesday" the rail's recency sort cannot answer. The
 * palette never holds anything exclusive: each row is a faster path to a
 * surface that also exists somewhere visible, so a user who never presses
 * ⌘K loses speed and nothing else.
 *
 * Creates that need a name take it here, in a second step, rather than
 * bouncing the user to the + Create menu to type it there.
 */
export type CommandPaletteProps = {
  open: boolean;
  close: () => void;
  conversations: Conversation[];
  openView: (view: View) => void;
  openThread: (conversationId: string) => void;
  create: (action: CreateAction, name: string, kind: DocumentKind) => Promise<void>;
};

export function CommandPalette({
  open,
  close,
  conversations,
  openView,
  openThread,
  create,
}: CommandPaletteProps) {
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState(0);
  // The create action waiting on a name, or null while searching.
  const [naming, setNaming] = useState<CreateAction | null>(null);
  const listRef = useRef<HTMLUListElement | null>(null);

  const rows = useMemo(() => buildPaletteRows(conversations), [conversations]);
  const matches = useMemo(() => matchPalette(rows, query), [rows, query]);

  // A fresh open is a fresh question.
  useEffect(() => {
    if (open) {
      setQuery("");
      setIndex(0);
      setNaming(null);
    }
  }, [open]);

  useEffect(() => {
    setIndex(0);
  }, [query]);

  useEffect(() => {
    listRef.current
      ?.querySelector('[data-focused="true"]')
      ?.scrollIntoView({ block: "nearest" });
  }, [index, matches]);

  if (!open) return null;

  async function run(row: PaletteRow) {
    if (row.kind === "view") {
      openView(row.view);
      close();
      return;
    }
    if (row.kind === "thread") {
      openThread(row.conversationId);
      close();
      return;
    }
    // A create that names itself later runs now; one that needs a name asks
    // for it in place — the input becomes the name field.
    if (!row.action.prompt) {
      close();
      await create(row.action, "", "markdown");
      return;
    }
    setNaming(row.action);
    setQuery("");
  }

  async function submitName() {
    if (!naming || !query.trim()) return;
    const action = naming;
    const name = query.trim();
    close();
    await create(action, name, "markdown");
  }

  return (
    <div className="palette-scrim" onClick={close}>
      <div
        className="palette"
        role="dialog"
        aria-label="Command palette"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="palette-input-row">
          {naming ? <Plus size={15} /> : <Search size={15} />}
          <input
            value={query}
            autoFocus
            placeholder={
              naming ? naming.prompt : "Jump to, create, or find a thread…"
            }
            aria-label={naming ? naming.prompt : "Command palette search"}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                event.preventDefault();
                // Escape backs out of naming before it closes the palette —
                // one step per press, like any nested surface.
                if (naming) {
                  setNaming(null);
                  setQuery("");
                } else {
                  close();
                }
                return;
              }
              if (naming) {
                if (event.key === "Enter") {
                  event.preventDefault();
                  void submitName();
                }
                return;
              }
              if (event.key === "ArrowDown") {
                event.preventDefault();
                setIndex((value) => Math.min(value + 1, matches.length - 1));
              } else if (event.key === "ArrowUp") {
                event.preventDefault();
                setIndex((value) => Math.max(value - 1, 0));
              } else if (event.key === "Enter") {
                event.preventDefault();
                const row = matches[index];
                if (row) void run(row);
              }
            }}
          />
          <kbd>esc</kbd>
        </div>
        {naming ? (
          <p className="palette-naming-hint">
            <CornerDownLeft size={12} aria-hidden="true" /> creates the{" "}
            {naming.noun}; esc goes back
          </p>
        ) : (
          <ul className="palette-list" role="listbox" aria-label="Results" ref={listRef}>
            {matches.length === 0 ? (
              <li className="palette-empty">Nothing matches.</li>
            ) : (
              matches.map((row, rowIndex) => (
                <li key={`${row.kind}-${row.label}-${rowIndex}`}>
                  <button
                    type="button"
                    role="option"
                    aria-selected={rowIndex === index}
                    data-focused={rowIndex === index || undefined}
                    className={rowIndex === index ? "palette-row focused" : "palette-row"}
                    onMouseEnter={() => setIndex(rowIndex)}
                    onClick={() => void run(row)}
                  >
                    {row.kind === "thread" ? (
                      <MessageSquare size={14} aria-hidden="true" />
                    ) : row.kind === "create" ? (
                      <Plus size={14} aria-hidden="true" />
                    ) : (
                      <Search size={14} aria-hidden="true" />
                    )}
                    <span className="palette-row-label">{row.label}</span>
                    <span className="palette-row-hint">{row.hint}</span>
                  </button>
                </li>
              ))
            )}
          </ul>
        )}
      </div>
    </div>
  );
}
