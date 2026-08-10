"use client";

import { Check, GripVertical, Pencil, Plus, Trash2, X } from "lucide-react";
import type { BoardColumn } from "@workspace/api-client";
import { useState } from "react";

/**
 * The structural half of a board: which columns exist, what they are called and
 * where cards sit inside them. Kept out of board.tsx so that file stays about
 * rendering cards and routing drags.
 *
 * Optional on the view's props: until the workspace wires these up the board
 * still renders and cards still move between columns, minus the column editing.
 */
export type BoardColumnOps = {
  addColumn: (boardId: string, name: string, index?: number) => Promise<void>;
  renameColumn: (boardId: string, columnId: string, name: string) => Promise<void>;
  deleteColumn: (
    boardId: string,
    columnId: string,
    moveCardsTo?: string,
  ) => Promise<void>;
  reorderColumns: (boardId: string, order: string[]) => Promise<void>;
  reorderCard: (
    boardId: string,
    cardId: string,
    index: number,
    columnId?: string,
  ) => Promise<void>;
};

/** What a drag is carrying. Cards and columns share one drop surface. */
export type DragPayload =
  | { kind: "card"; cardId: string; columnId: string; index: number }
  | { kind: "column"; columnId: string; index: number };

const DRAG_TYPE = "application/x-board-item";

export function writeDrag(event: React.DragEvent, payload: DragPayload) {
  event.dataTransfer.setData(DRAG_TYPE, JSON.stringify(payload));
  // Plain text keeps the pre-existing card drop path working for anything that
  // only reads text/plain.
  if (payload.kind === "card") event.dataTransfer.setData("text/plain", payload.cardId);
  event.dataTransfer.effectAllowed = "move";
}

export function readDrag(event: React.DragEvent): DragPayload | null {
  const raw = event.dataTransfer.getData(DRAG_TYPE);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as DragPayload;
    return parsed.kind === "card" || parsed.kind === "column" ? parsed : null;
  } catch {
    return null;
  }
}

/**
 * Where a dragged item lands, given the pointer and the element it is over.
 * Past the midpoint means "after this one".
 */
export function slotFrom(
  event: React.DragEvent,
  element: HTMLElement,
  index: number,
  axis: "y" | "x" = "y",
): number {
  const box = element.getBoundingClientRect();
  const past =
    axis === "y"
      ? event.clientY > box.top + box.height / 2
      : event.clientX > box.left + box.width / 2;
  return past ? index + 1 : index;
}

/**
 * The list the server should end up with, after taking `id` out of `order` and
 * dropping it at `slot` — `slot` is an index into the list *including* `id`.
 */
export function reindex(order: string[], id: string, slot: number): string[] {
  const from = order.indexOf(id);
  const rest = order.filter((item) => item !== id);
  const to = from >= 0 && from < slot ? slot - 1 : slot;
  rest.splice(Math.max(0, Math.min(to, rest.length)), 0, id);
  return rest;
}

/** Index within a column once the dragged card itself is discounted. */
export function cardSlot(
  cardIds: string[],
  cardId: string,
  slot: number,
  sameColumn: boolean,
): number {
  if (!sameColumn) return Math.max(0, Math.min(slot, cardIds.length));
  const from = cardIds.indexOf(cardId);
  const to = from >= 0 && from < slot ? slot - 1 : slot;
  return Math.max(0, Math.min(to, cardIds.length - 1));
}

export function DropLine({ active }: { active: boolean }) {
  return (
    <div
      aria-hidden
      style={{
        height: 2,
        borderRadius: 2,
        background: active ? "var(--accent)" : "transparent",
      }}
    />
  );
}

export function AddColumn({ onAdd }: { onAdd: (name: string) => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");

  if (!open) {
    return (
      <button className="kanban-add" onClick={() => setOpen(true)}>
        <Plus size={14} /> Add column
      </button>
    );
  }
  return (
    <form
      className="kanban-add-form"
      onSubmit={async (event) => {
        event.preventDefault();
        if (!name.trim()) return;
        await onAdd(name.trim());
        setName("");
        setOpen(false);
      }}
    >
      <input
        value={name}
        onChange={(event) => setName(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Escape") setOpen(false);
        }}
        placeholder="Column name"
        autoFocus
      />
      <div className="kanban-add-actions">
        <button type="button" className="ghost-button" onClick={() => setOpen(false)}>
          Cancel
        </button>
        <button type="submit" className="primary-button">
          Add
        </button>
      </div>
    </form>
  );
}

type ColumnHeaderProps = {
  column: BoardColumn;
  /** The other columns, offered as destinations when a full column is deleted. */
  siblings: BoardColumn[];
  onRename?: (name: string) => Promise<void>;
  onDelete?: (moveCardsTo: string) => Promise<void>;
};

export function ColumnHeader({
  column,
  siblings,
  onRename,
  onDelete,
}: ColumnHeaderProps) {
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(column.name);
  const [confirming, setConfirming] = useState(false);
  const [destination, setDestination] = useState(siblings[0]?.id ?? "");
  const editable = Boolean(onRename && onDelete);

  if (renaming && onRename) {
    return (
      <form
        className="kanban-add-form"
        onSubmit={async (event) => {
          event.preventDefault();
          if (!name.trim()) return;
          await onRename(name.trim());
          setRenaming(false);
        }}
      >
        <input
          value={name}
          onChange={(event) => setName(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              setName(column.name);
              setRenaming(false);
            }
          }}
          aria-label={`Rename ${column.name}`}
          autoFocus
        />
        <div className="kanban-add-actions">
          <button
            type="button"
            className="ghost-button"
            onClick={() => {
              setName(column.name);
              setRenaming(false);
            }}
            aria-label={`Cancel renaming ${column.name}`}
          >
            <X size={13} />
          </button>
          <button
            type="submit"
            className="primary-button"
            aria-label={`Save name for ${column.name}`}
          >
            <Check size={13} />
          </button>
        </div>
      </form>
    );
  }

  // A column holding cards is never deleted on one click: the cards have to be
  // given somewhere to go, which is also what the API demands.
  if (confirming && onDelete) {
    const needsDestination = column.cards.length > 0;
    return (
      <form
        className="kanban-add-form"
        onSubmit={async (event) => {
          event.preventDefault();
          if (needsDestination && !destination) return;
          await onDelete(needsDestination ? destination : "");
          setConfirming(false);
        }}
      >
        {needsDestination && (
          <select
            value={destination}
            onChange={(event) => setDestination(event.target.value)}
            aria-label={`Move ${column.cards.length} cards to`}
          >
            {siblings.map((item) => (
              <option key={item.id} value={item.id}>
                Move {column.cards.length} to {item.name}
              </option>
            ))}
          </select>
        )}
        <div className="kanban-add-actions">
          <button
            type="button"
            className="ghost-button"
            onClick={() => setConfirming(false)}
          >
            Cancel
          </button>
          <button type="submit" className="primary-button">
            Delete column
          </button>
        </div>
      </form>
    );
  }

  return (
    <div className="kanban-column-head">
      {editable && <GripVertical size={13} aria-hidden />}
      <span>{column.name}</span>
      <span className="kanban-count">{column.cards.length}</span>
      {editable && (
        <>
          <button
            className="icon-button"
            onClick={() => {
              setName(column.name);
              setRenaming(true);
            }}
            aria-label={`Rename ${column.name}`}
          >
            <Pencil size={12} />
          </button>
          <button
            className="icon-button"
            onClick={() => {
              setDestination(siblings[0]?.id ?? "");
              setConfirming(true);
            }}
            disabled={siblings.length === 0}
            aria-label={`Delete ${column.name}`}
          >
            <Trash2 size={12} />
          </button>
        </>
      )}
    </div>
  );
}
