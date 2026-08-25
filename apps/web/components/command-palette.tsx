"use client";

import type {
  Conversation,
  ConversationSearchHit,
  DocumentKind,
} from "@workspace/api-client";
import { Columns2, CornerDownLeft, MessageSquare, Plus, Search, Settings2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { CreateAction } from "./views/navigation";
import {
  buildPaletteRows,
  matchPalette,
  type PaletteExtras,
  type PaletteRow,
  type PaletteToggle,
} from "./views/palette";
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
  /**
   * Open a thread beside the primary instead of as it: ⌘Enter or ⌘click on a
   * thread row. Optional so the palette stands without the split; the rows
   * only advertise the modifier when it is wired.
   */
  openThreadInSplit?: (conversationId: string) => void;
  create: (action: CreateAction, name: string, kind: DocumentKind) => Promise<void>;
  /**
   * Deep search: what was SAID, not only what things are named. Optional so
   * the palette stands without the index; when present it is queried after a
   * pause, and its hits render under the instant title matches.
   */
  searchTranscripts?: (q: string) => Promise<ConversationSearchHit[]>;
  /**
   * The shell state behind the layout and preference rows. Optional like the
   * split: without it the palette simply has no such rows, so it still stands
   * in tests and simpler hosts. `extras.threadOpen` also steers the thread
   * rows' Enter — "split" makes the split the default and ⌘⏎ the way back.
   */
  extras?: PaletteExtras;
  /** Recall the named layout: Enter on its row. */
  applyLayout?: (name: string) => void;
  /** Capture the current split under a name: the "Save layout as…" row. */
  saveLayout?: (name: string) => void;
  /** Forget the named layout: ⌘⌫ while its row is focused. */
  deleteLayout?: (name: string) => void;
  /** Flip (and persist) the named preference: Enter on its toggle row. */
  togglePreference?: (toggle: PaletteToggle) => void;
};

/**
 * The step waiting on a name, generalized from the Create actions so "Save
 * layout as…" reuses it: what the input asks for, what the hint calls the
 * result, what the verb is, and what Enter does with the trimmed name.
 */
type NamingTask = {
  prompt: string;
  noun: string;
  verb: string;
  submit: (name: string) => void | Promise<void>;
};

export function CommandPalette({
  open,
  close,
  conversations,
  openView,
  openThread,
  openThreadInSplit,
  create,
  searchTranscripts,
  extras,
  applyLayout,
  saveLayout,
  deleteLayout,
  togglePreference,
}: CommandPaletteProps) {
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState(0);
  // The task waiting on a name, or null while searching.
  const [naming, setNaming] = useState<NamingTask | null>(null);
  const listRef = useRef<HTMLUListElement | null>(null);

  const rows = useMemo(() => buildPaletteRows(conversations, extras), [conversations, extras]);
  const instant = useMemo(() => matchPalette(rows, query), [rows, query]);

  // Deep hits arrive late and never reorder the instant rows above them: a
  // list that reshuffles under a moving selection is how ⌘K users open the
  // wrong thing. Debounced, three characters minimum, stale replies dropped.
  const [deepHits, setDeepHits] = useState<ConversationSearchHit[]>([]);
  useEffect(() => {
    if (!open || naming || !searchTranscripts || query.trim().length < 3) {
      setDeepHits([]);
      return;
    }
    let stale = false;
    const timer = window.setTimeout(() => {
      searchTranscripts(query.trim())
        .then((hits) => {
          if (!stale) setDeepHits(hits);
        })
        .catch(() => undefined); // no index, no row — not an error state
    }, 200);
    return () => {
      stale = true;
      window.clearTimeout(timer);
    };
  }, [open, naming, query, searchTranscripts]);

  const matches: PaletteRow[] = useMemo(() => {
    const titled = new Set(
      instant
        .filter((row) => row.kind === "thread")
        .map((row) => (row as Extract<PaletteRow, { kind: "thread" }>).conversationId),
    );
    const seen = new Set<string>();
    const deep: PaletteRow[] = [];
    for (const hit of deepHits) {
      if (titled.has(hit.conversation_id) || seen.has(hit.conversation_id)) continue;
      seen.add(hit.conversation_id);
      deep.push({
        kind: "thread",
        conversationId: hit.conversation_id,
        label: hit.title,
        hint: `“${hit.snippet.slice(0, 80)}…”`,
      });
      if (deep.length >= 5) break;
    }
    return [...instant, ...deep];
  }, [instant, deepHits]);

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

  async function run(row: PaletteRow, split = false) {
    if (row.kind === "view") {
      openView(row.view);
      close();
      return;
    }
    if (row.kind === "thread") {
      // ⌘Enter / ⌘click: the other destination, whichever way the thread-open
      // preference points — beside the primary by default, in its place when
      // "split" is already the default. Falls back to the plain open when no
      // split is wired, so neither gesture ever no-ops.
      const inSplit = split !== (extras?.threadOpen === "split");
      if (inSplit && openThreadInSplit) openThreadInSplit(row.conversationId);
      else openThread(row.conversationId);
      close();
      return;
    }
    if (row.kind === "layout") {
      // ⌘Enter / ⌘click forgets the layout instead of applying it — the same
      // modifier gesture ⌘⌫ offers the keyboard, so a pointer user is not
      // locked out of deletion. The palette stays open so the list is seen
      // to shrink; a plain activation applies and closes.
      if (split && deleteLayout) {
        deleteLayout(row.name);
        // The list is about to lose a row and the palette stays open: clamp
        // the focus like the ⌘⌫ path does, or deleting the last row leaves
        // the selection one past the end and the next Enter does nothing.
        setIndex((value) => Math.max(0, Math.min(value, matches.length - 2)));
        return;
      }
      applyLayout?.(row.name);
      close();
      return;
    }
    if (row.kind === "save-layout") {
      setNaming({
        prompt: "Layout name",
        noun: "layout",
        verb: "saves",
        submit: (name) => saveLayout?.(name),
      });
      setQuery("");
      return;
    }
    if (row.kind === "toggle") {
      togglePreference?.(row.toggle);
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
    const action = row.action;
    setNaming({
      prompt: action.prompt,
      noun: action.noun,
      verb: "creates",
      submit: (name) => create(action, name, "markdown"),
    });
    setQuery("");
  }

  async function submitName() {
    if (!naming || !query.trim()) return;
    const task = naming;
    const name = query.trim();
    close();
    await task.submit(name);
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
                if (row) void run(row, event.metaKey || event.ctrlKey);
              } else if (event.key === "Backspace" && (event.metaKey || event.ctrlKey)) {
                // ⌘⌫ on a focused layout row forgets the layout, in place —
                // the row's hint advertises it, and the palette stays open so
                // the list is seen to shrink. Any other focus keeps the key:
                // clearing the input line is still what ⌘⌫ means in a field.
                const row = matches[index];
                if (row && row.kind === "layout" && deleteLayout) {
                  event.preventDefault();
                  deleteLayout(row.name);
                  // The list is about to lose a row; keep the focus in bounds.
                  setIndex((value) => Math.max(0, Math.min(value, matches.length - 2)));
                }
              }
            }}
          />
          <kbd>esc</kbd>
        </div>
        {naming ? (
          <p className="palette-naming-hint">
            <CornerDownLeft size={12} aria-hidden="true" /> {naming.verb} the{" "}
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
                    onClick={(event) => void run(row, event.metaKey || event.ctrlKey)}
                  >
                    {row.kind === "thread" ? (
                      <MessageSquare size={14} aria-hidden="true" />
                    ) : row.kind === "create" || row.kind === "save-layout" ? (
                      <Plus size={14} aria-hidden="true" />
                    ) : row.kind === "layout" ? (
                      <Columns2 size={14} aria-hidden="true" />
                    ) : row.kind === "toggle" ? (
                      <Settings2 size={14} aria-hidden="true" />
                    ) : (
                      <Search size={14} aria-hidden="true" />
                    )}
                    <span className="palette-row-label">{row.label}</span>
                    <span className="palette-row-hint">
                      {row.kind === "thread" && openThreadInSplit
                        ? // The modifier's meaning follows the preference: it is
                          // always the OTHER way a thread can open.
                          `${row.hint} · ⌘⏎ ${extras?.threadOpen === "split" ? "in place" : "split"}`
                        : row.kind === "layout" && deleteLayout
                          ? `${row.hint} · ⌘⌫ deletes`
                          : row.hint}
                    </span>
                    {row.kind === "view" && row.shortcut && (
                      <kbd className="palette-row-shortcut">{row.shortcut}</kbd>
                    )}
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
