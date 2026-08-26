"use client";

import { ChevronDown, ChevronUp, KanbanSquare, ListChecks, Plus, Trash2 } from "lucide-react";
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
import { glyphFor, isTodoList } from "./todo-format";
import { TodoChecklist, type TodoOps } from "./todos";

export type BoardViewProps = {
  /** Every board, lists included — this page is the one listing for both. */
  boards: Board[];
  createBoard: (name: string) => Promise<void>;
  addCard: (boardId: string, column: string, title: string) => Promise<void>;
  moveCard: (boardId: string, cardId: string, column: string) => Promise<void>;
  removeCard: (boardId: string, cardId: string) => Promise<void>;
  removeBoard: (board: Board) => Promise<void>;
  columnOps?: BoardColumnOps;
  todoOps: TodoOps;
  /** Who is looking, for the claim chips — see TodoChecklistProps.selfId. */
  selfId?: string;
};

/** The glyph an entry wears — the whole visible difference between the shapes. */
function Glyph({ board }: { board: Board }) {
  const Icon = glyphFor(board) === "list" ? ListChecks : KanbanSquare;
  return <Icon size={15} aria-hidden className="board-glyph" />;
}

/**
 * The one deletion gate, for both shapes. A list and a board are the same
 * object, so one click must not be able to destroy the one and only warn on
 * the other; the copy names the thing and what it takes with it.
 */
function confirmRemoval(board: Board): boolean {
  const count = board.columns.reduce((total, column) => total + column.cards.length, 0);
  const cargo = isTodoList(board)
    ? `${count} item${count === 1 ? "" : "s"}`
    : `${count} card${count === 1 ? "" : "s"}`;
  return window.confirm(`Delete “${board.name}” and its ${cargo}?`);
}

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
        aria-label="Card title"
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

/**
 * The keyboard route to a cross-column drag, in two steps: the select only
 * states a destination, the button beside it commits. Matching the column
 * header's delete-with-destination form — and unlike a bare select that
 * committed onChange, which moved the card on the first ArrowDown of a
 * keyboard user still browsing the options.
 */
function MoveCard({
  cardTitle,
  siblings,
  onMove,
}: {
  cardTitle: string;
  siblings: { id: string; name: string }[];
  onMove: (destination: string) => Promise<void>;
}) {
  const [destination, setDestination] = useState("");
  return (
    <>
      <select
        className="kanban-move"
        value={destination}
        aria-label={`Move ${cardTitle} to`}
        onChange={(event) => setDestination(event.target.value)}
      >
        <option value="">Move to…</option>
        {siblings.map((item) => (
          <option key={item.id} value={item.name}>
            {item.name}
          </option>
        ))}
      </select>
      <button
        type="button"
        className="ghost-button"
        aria-label={`Move ${cardTitle}`}
        onClick={() => {
          if (!destination) return;
          setDestination("");
          void onMove(destination);
        }}
      >
        Move
      </button>
    </>
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
  // A second chevron press before the reorder lands would be computed from the
  // stale index and silently lost — same guard the column chevrons carry, and
  // aria-disabled for the same reason: focus must survive the flight.
  const [reordering, setReordering] = useState(false);

  async function stepCard(cardId: string, index: number, columnId: string) {
    if (!ops || reordering) return;
    setReordering(true);
    try {
      await ops.reorderCard(board.id, cardId, index, columnId);
    } finally {
      setReordering(false);
    }
  }

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
                // The keyboard route to what the drag does, through the same
                // reindex the drop handler trusts. Null at an edge: the button
                // stays, aria-disabled but focusable, so focus survives
                // reaching either end.
                move={
                  ops
                    ? {
                        left:
                          columnIndex > 0
                            ? () =>
                                ops.reorderColumns(
                                  board.id,
                                  reindex(
                                    board.columns.map((item) => item.id),
                                    column.id,
                                    columnIndex - 1,
                                  ),
                                )
                            : null,
                        right:
                          columnIndex < board.columns.length - 1
                            ? () =>
                                ops.reorderColumns(
                                  board.id,
                                  reindex(
                                    board.columns.map((item) => item.id),
                                    column.id,
                                    columnIndex + 2,
                                  ),
                                )
                            : null,
                      }
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
                      // Same bail as the column header: a press-and-slide on
                      // the card's own controls must not start a card drag.
                      if (
                        (event.target as HTMLElement).closest(
                          "input, select, button, textarea",
                        )
                      ) {
                        event.preventDefault();
                        return;
                      }
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
                      {/* The keyboard route to an in-column drag. aria-disabled
                          at the edges, matching the column chevrons: the button
                          stays focusable so focus survives reaching either
                          end. */}
                      {ops && (
                        <>
                          <button
                            className="icon-button"
                            aria-disabled={cardIndex === 0 || reordering}
                            aria-label={`Move ${card.title} up`}
                            onClick={() => {
                              if (cardIndex === 0) return;
                              void stepCard(card.id, cardIndex - 1, column.id);
                            }}
                          >
                            <ChevronUp size={12} />
                          </button>
                          <button
                            className="icon-button"
                            aria-disabled={
                              cardIndex === column.cards.length - 1 || reordering
                            }
                            aria-label={`Move ${card.title} down`}
                            onClick={() => {
                              if (cardIndex === column.cards.length - 1) return;
                              void stepCard(card.id, cardIndex + 1, column.id);
                            }}
                          >
                            <ChevronDown size={12} />
                          </button>
                        </>
                      )}
                      {siblings.length > 0 && (
                        <MoveCard
                          cardTitle={card.title}
                          siblings={siblings}
                          onMove={(destination) =>
                            moveCard(board.id, card.id, destination)
                          }
                        />
                      )}
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

/**
 * The one listing for boards and todo lists both.
 *
 * A list is a board with one column, and until now that rule split the objects
 * across two pages — so a one-column board silently *teleported* between
 * "Lists" and "Boards" the moment its column count crossed 1. Here the object
 * stays put: crossing the threshold changes its glyph (and the shell raises a
 * notice saying so), never its place in the listing.
 */
export function BoardView({
  boards,
  createBoard,
  addCard,
  moveCard,
  removeCard,
  removeBoard,
  columnOps,
  todoOps,
  selfId,
}: BoardViewProps) {
  const [name, setName] = useState("");
  const [shape, setShape] = useState<"board" | "list">("board");

  // The list entry's delete goes through TodoChecklist's own button, so the
  // shared gate is threaded into the ops it calls: both shapes meet the same
  // confirm, and neither is destroyed on a single click.
  const gatedTodoOps: TodoOps = {
    ...todoOps,
    removeTodoList: async (list) => {
      if (confirmRemoval(list)) await todoOps.removeTodoList(list);
    },
  };

  return (
    <div className="content-page">
      <div className="page-heading">
        <h1>Boards & todos</h1>
        <form
          className="board-new"
          onSubmit={async (event) => {
            event.preventDefault();
            const value = name.trim();
            if (!value) return;
            setName("");
            // Two shapes of the same thing: a board is born with three
            // columns, a list with one. Nothing else differs at birth.
            if (shape === "list") await todoOps.createTodoList(value);
            else await createBoard(value);
          }}
        >
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            aria-label={shape === "list" ? "List name" : "Board name"}
          />
          <select
            value={shape}
            onChange={(event) => setShape(event.target.value as "board" | "list")}
            aria-label="Create as"
          >
            <option value="board">Board</option>
            <option value="list">List</option>
          </select>
          <button type="submit" className="primary-button" disabled={!name.trim()}>
            <Plus size={15} /> Create
          </button>
        </form>
      </div>

      {boards.length === 0 ? (
        <div className="empty-state">
          {/* Says where these come from rather than only that there are none:
              the interesting way to get one is to ask for it in chat. */}
          <p>No boards or lists yet. Make one here, or ask the assistant to track something.</p>
        </div>
      ) : (
        boards.map((board) =>
          isTodoList(board) ? (
            <section key={board.id} className="board" data-shape={glyphFor(board)}>
              <TodoChecklist list={board} ops={gatedTodoOps} selfId={selfId} />
              {/* The graduation affordance: the same Add column a board offers.
                  Grow a second column and this entry redraws as a board — same
                  place, same items, ticks intact. */}
              {columnOps && (
                <AddColumn onAdd={(columnName) => columnOps.addColumn(board.id, columnName)} />
              )}
            </section>
          ) : (
            <section key={board.id} className="board" data-shape={glyphFor(board)}>
              <header className="board-head">
                <Glyph board={board} />
                <h2>{board.name}</h2>
                <button
                  className="icon-button"
                  onClick={() => {
                    if (confirmRemoval(board)) void removeBoard(board);
                  }}
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
          ),
        )
      )}
    </div>
  );
}
