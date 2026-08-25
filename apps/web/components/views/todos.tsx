"use client";

import { ListChecks, Plus, Trash2 } from "lucide-react";
import type { Board } from "@workspace/api-client";
import { useState } from "react";
import { describeProgress, itemsOf } from "./todo-format";

/**
 * Todo lists — a one-column board, drawn as checkboxes.
 *
 * `TodoChecklist` is the whole feature and it is mounted twice: on the merged
 * Boards & todos page, and inline in a conversation under the tool call that
 * touched a list. That is the point of writing it once. The chat mount is the
 * one that matters — "track these three things" should produce something
 * checkable *where it was asked for*, not a link to a page — and if the two
 * were separate components the one in chat would be the one that quietly
 * rotted.
 */

export type TodoOps = {
  createTodoList: (name: string) => Promise<void>;
  addTodoItem: (list: Board, title: string) => Promise<void>;
  setTodoItemDone: (list: Board, itemId: string, done: boolean) => Promise<void>;
  removeTodoItem: (list: Board, itemId: string) => Promise<void>;
  removeTodoList: (list: Board) => Promise<void>;
};

function AddItem({ onAdd, label }: { onAdd: (title: string) => Promise<void>; label: string }) {
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");

  if (!open) {
    return (
      <button type="button" className="todo-add" onClick={() => setOpen(true)}>
        <Plus size={14} /> {label}
      </button>
    );
  }
  return (
    <form
      className="todo-add-form"
      onSubmit={async (event) => {
        event.preventDefault();
        const value = title.trim();
        if (!value) return;
        setTitle("");
        await onAdd(value);
      }}
    >
      <input
        value={title}
        onChange={(event) => setTitle(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Escape") setOpen(false);
        }}
        aria-label={label}
        autoFocus
      />
      <button type="submit" className="primary-button" disabled={!title.trim()}>
        Add
      </button>
      <button type="button" className="ghost-button" onClick={() => setOpen(false)}>
        Done
      </button>
    </form>
  );
}

export type TodoChecklistProps = {
  list: Board;
  ops: TodoOps;
  /**
   * Said above the checkboxes when the list is being shown because something
   * happened to it — in chat, that the assistant is what happened. Omitted on
   * the Lists page, where the list is simply where it lives.
   */
  caption?: string;
  /** Chat renders a denser card with no delete affordances. */
  compact?: boolean;
};

export function TodoChecklist({ list, ops, caption, compact }: TodoChecklistProps) {
  const items = itemsOf(list);

  return (
    <section className={compact ? "todo-list compact" : "todo-list"}>
      <header className="todo-list-head">
        {/* The list's glyph in the merged listing — a board of the same name
            wears a kanban square here. Chat's compact card skips it: inline,
            the caption already says what this is. */}
        {!compact && <ListChecks size={15} aria-hidden className="board-glyph" />}
        <div>
          <h2>{list.name}</h2>
          <span className="todo-progress">{describeProgress(list)}</span>
        </div>
        {!compact && (
          <button
            className="icon-button"
            onClick={() => void ops.removeTodoList(list)}
            aria-label={`Delete ${list.name}`}
          >
            <Trash2 size={15} />
          </button>
        )}
      </header>
      {caption && <p className="todo-caption">{caption}</p>}
      <ul className="todo-items">
        {items.map((item) => (
          <li key={item.id} className={item.done ? "todo-item done" : "todo-item"}>
            <label>
              {/* Not disabled while the write is in flight: the tick is
                  applied before the round trip and rolled back if the server
                  refuses, so there is nothing to wait for. A checkbox that
                  freezes on click is one people click twice. */}
              <input
                type="checkbox"
                checked={item.done}
                onChange={(event) =>
                  void ops.setTodoItemDone(list, item.id, event.target.checked)
                }
              />
              <span className="todo-item-title">{item.title}</span>
            </label>
            {!compact && (
              <button
                className="todo-item-delete"
                onClick={() => void ops.removeTodoItem(list, item.id)}
                aria-label={`Delete ${item.title}`}
              >
                <Trash2 size={12} />
              </button>
            )}
          </li>
        ))}
      </ul>
      <AddItem
        label={compact ? "Add to this list" : "Add item"}
        onAdd={(title) => ops.addTodoItem(list, title)}
      />
    </section>
  );
}
