"use client";

import { KanbanSquare, Plus, Trash2 } from "lucide-react";
import type { Board } from "@workspace/api-client";
import { useState } from "react";
import {
  AddColumn,
  type BoardColumnOps,
  ColumnHeader,
  type DragPayload,
  DropLine,
  cardSlot,
  readDrag,
  reindex,
  slotFrom,
  writeDrag,
} from "./board-columns";

export type BoardViewProps = {
  boards: Board[];
  createBoard: (name: string) => Promise<void>;
  addCard: (boardId: string, column: string, title: string) => Promise<void>;
  moveCard: (boardId: string, cardId: string, column: string) => Promise<void>;
  removeCard: (boardId: string, cardId: string) => Promise<void>;
  removeBoard: (board: Board) => Promise<void>;
  columnOps?: BoardColumnOps;
};

function AddCard({
  onAdd,
}: {
  onAdd: (title: string) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");

  if (!open) {
    return (
      <button className="kanban-add" onClick={() => setOpen(true)}>
        <Plus size={14} /> Add card
      </button>
    );
  }
  return (
    <form
      className="kanban-add-form"
      onSubmit={async (event) => {
        event.preventDefault();
        if (!title.trim()) return;
        await onAdd(title.trim());
        setTitle("");
        setOpen(false);
      }}
    >
      <textarea
        value={title}
        onChange={(event) => setTitle(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            event.currentTarget.form?.requestSubmit();
          }
          if (event.key === "Escape") setOpen(false);
        }}
        rows={2}
        placeholder="Card title"
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

type CardTarget = { columnId: string; slot: number };

function BoardCanvas({
  board,
  addCard,
  moveCard,
  removeCard,
  ops,
}: {
  board: Board;
  addCard: BoardViewProps["addCard"];
  moveCard: BoardViewProps["moveCard"];
  removeCard: BoardViewProps["removeCard"];
  ops?: BoardColumnOps;
}) {
  // dataTransfer is unreadable during dragover, so the payload is mirrored in
  // state to decide what the pointer is currently hovering over.
  const [drag, setDrag] = useState<DragPayload | null>(null);
  const [cardTarget, setCardTarget] = useState<CardTarget | null>(null);
  const [columnTarget, setColumnTarget] = useState<number | null>(null);

  function clear() {
    setDrag(null);
    setCardTarget(null);
    setColumnTarget(null);
  }

  async function dropCard(payload: DragPayload, columnId: string, slot: number) {
    if (payload.kind !== "card") return;
    const column = board.columns.find((item) => item.id === columnId);
    if (!column) return;
    const sameColumn = payload.columnId === columnId;
    if (!ops) {
      // Without the ordering endpoints wired up, a cross-column drag is still a
      // plain move; an in-column drag has nowhere to go.
      if (!sameColumn) await moveCard(board.id, payload.cardId, column.name);
      return;
    }
    const ids = column.cards.map((card) => card.id);
    const index = cardSlot(ids, payload.cardId, slot, sameColumn);
    if (sameColumn && index === ids.indexOf(payload.cardId)) return;
    await ops.reorderCard(board.id, payload.cardId, index, columnId);
  }

  async function dropColumn(payload: DragPayload, slot: number) {
    if (payload.kind !== "column" || !ops) return;
    const order = board.columns.map((column) => column.id);
    const next = reindex(order, payload.columnId, slot);
    if (next.join() === order.join()) return;
    await ops.reorderColumns(board.id, next);
  }

  return (
    <div className="kanban">
      {board.columns.map((column, columnIndex) => {
        const siblings = board.columns.filter((item) => item.id !== column.id);
        const insertBefore = columnTarget === columnIndex;
        const insertAfter = columnTarget === columnIndex + 1;
        return (
          <div
            key={column.id}
            className="kanban-column"
            style={
              drag?.kind === "column" && (insertBefore || insertAfter)
                ? {
                    boxShadow: `inset ${insertBefore ? "2px" : "-2px"} 0 0 var(--accent)`,
                  }
                : undefined
            }
            onDragOver={(event) => {
              event.preventDefault();
              if (drag?.kind === "column") {
                setColumnTarget(slotFrom(event, event.currentTarget, columnIndex, "x"));
              } else if (drag?.kind === "card") {
                // Only fires over the column's empty space — cards stop the
                // event themselves — so this means "put it last".
                setCardTarget({ columnId: column.id, slot: column.cards.length });
              }
            }}
            onDragLeave={(event) => {
              if (event.currentTarget.contains(event.relatedTarget as Node)) return;
              if (cardTarget?.columnId === column.id) setCardTarget(null);
            }}
            onDrop={async (event) => {
              event.preventDefault();
              const payload = readDrag(event) ?? drag;
              const target = cardTarget;
              const columnSlot = columnTarget;
              clear();
              if (!payload) return;
              if (payload.kind === "column") {
                await dropColumn(payload, columnSlot ?? columnIndex);
              } else {
                await dropCard(
                  payload,
                  target?.columnId ?? column.id,
                  target?.slot ?? column.cards.length,
                );
              }
            }}
          >
            <div
              draggable={Boolean(ops)}
              onDragStart={(event) => {
                // Selecting text in the rename field must not drag the column.
                if (
                  (event.target as HTMLElement).closest("input, select, button, textarea")
                ) {
                  event.preventDefault();
                  return;
                }
                const payload: DragPayload = {
                  kind: "column",
                  columnId: column.id,
                  index: columnIndex,
                };
                writeDrag(event, payload);
                setDrag(payload);
              }}
              onDragEnd={clear}
            >
              <ColumnHeader
                column={column}
                siblings={siblings}
                onRename={
                  ops
                    ? (name) => ops.renameColumn(board.id, column.id, name)
                    : undefined
                }
                onDelete={
                  ops
                    ? (moveCardsTo) =>
                        ops.deleteColumn(board.id, column.id, moveCardsTo || undefined)
                    : undefined
                }
              />
            </div>
            <div className="kanban-cards">
              {column.cards.map((card, cardIndex) => (
                <div key={card.id}>
                  <DropLine
                    active={
                      drag?.kind === "card" &&
                      cardTarget?.columnId === column.id &&
                      cardTarget.slot === cardIndex
                    }
                  />
                  <article
                    className="kanban-card"
                    draggable
                    onDragStart={(event) => {
                      const payload: DragPayload = {
                        kind: "card",
                        cardId: card.id,
                        columnId: column.id,
                        index: cardIndex,
                      };
                      writeDrag(event, payload);
                      setDrag(payload);
                    }}
                    onDragEnd={clear}
                    onDragOver={(event) => {
                      if (drag?.kind !== "card") return;
                      event.preventDefault();
                      event.stopPropagation();
                      setCardTarget({
                        columnId: column.id,
                        slot: slotFrom(event, event.currentTarget, cardIndex),
                      });
                    }}
                  >
                    <div className="kanban-card-title">{card.title}</div>
                    {card.body && <p>{card.body}</p>}
                    <div className="kanban-card-foot">
                      {card.labels.map((label) => (
                        <span key={label} className="kanban-label">
                          {label}
                        </span>
                      ))}
                      <button
                        className="kanban-delete"
                        onClick={() => void removeCard(board.id, card.id)}
                        aria-label={`Delete ${card.title}`}
                      >
                        <Trash2 size={12} />
                      </button>
                    </div>
                  </article>
                </div>
              ))}
              <DropLine
                active={
                  drag?.kind === "card" &&
                  cardTarget?.columnId === column.id &&
                  cardTarget.slot === column.cards.length
                }
              />
            </div>
            <AddCard onAdd={(title) => addCard(board.id, column.name, title)} />
          </div>
        );
      })}
      {ops && (
        <div className="kanban-column">
          <AddColumn onAdd={(name) => ops.addColumn(board.id, name)} />
        </div>
      )}
    </div>
  );
}

export function BoardView({
  boards,
  createBoard,
  addCard,
  moveCard,
  removeCard,
  removeBoard,
  columnOps,
}: BoardViewProps) {
  const [name, setName] = useState("");

  return (
    <div className="content-page">
      <div className="page-heading">
        <h1>Boards</h1>
        <form
          className="board-new"
          onSubmit={async (event) => {
            event.preventDefault();
            if (!name.trim()) return;
            await createBoard(name.trim());
            setName("");
          }}
        >
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="New board name"
          />
          <button type="submit" className="primary-button" disabled={!name.trim()}>
            <Plus size={15} /> Create
          </button>
        </form>
      </div>

      {boards.length === 0 ? (
        <div className="empty-state">
          <KanbanSquare size={22} />
          <p>No boards yet. Create one, or ask the assistant to plan your work.</p>
        </div>
      ) : (
        boards.map((board) => (
          <section key={board.id} className="board">
            <header className="board-head">
              <h2>{board.name}</h2>
              <button
                className="icon-button"
                onClick={() => void removeBoard(board)}
                aria-label={`Delete ${board.name}`}
              >
                <Trash2 size={15} />
              </button>
            </header>
            <BoardCanvas
              board={board}
              addCard={addCard}
              moveCard={moveCard}
              removeCard={removeCard}
              ops={columnOps}
            />
          </section>
        ))
      )}
    </div>
  );
}
