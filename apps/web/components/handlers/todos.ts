"use client";

import type { Board } from "@workspace/api-client";
import type { Dispatch, SetStateAction } from "react";
import { api } from "../api";
import { describeError } from "../views/shared";

/**
 * Todo lists, over the board state the shell already keeps.
 *
 * Only two of these reach a `/api/todos` route, and they are exactly the two a
 * board route cannot express: making a list (which has to know what shape a
 * list is born in) and ticking an item off *without naming its board*. Adding,
 * deleting and removing a list are board operations on a board, so they go
 * through the board handlers' own endpoints and come back as a whole Board —
 * which is also why a list never needs a second slice of state.
 */
export type TodoHandlerDeps = {
  setError: Dispatch<SetStateAction<string>>;
  setBoards: Dispatch<SetStateAction<Board[]>>;
};

export function createTodoHandlers({ setError, setBoards }: TodoHandlerDeps) {
  function replaceBoard(board: Board) {
    setBoards((items) => items.map((item) => (item.id === board.id ? board : item)));
  }

  /** A list's sole column. Empty only for a board that is not a list. */
  function soleColumn(list: Board): string {
    return list.columns[0]?.name ?? "";
  }

  async function createTodoList(name: string) {
    setError("");
    try {
      await api.createTodoList(name);
      // Refetched rather than patched in: the response is a TodoList and the
      // state holds Boards, and inventing the column id the server just
      // generated is how the two shapes drift.
      setBoards(await api.listBoards());
    } catch (caught) {
      setError(describeError(caught, "Could not create that list"));
    }
  }

  async function addTodoItem(list: Board, title: string) {
    const column = soleColumn(list);
    if (!column) return;
    try {
      replaceBoard(await api.addBoardCard(list.id, column, title));
    } catch (caught) {
      setError(describeError(caught, "Could not add that item"));
    }
  }

  /**
   * Tick one item, in place.
   *
   * The route returns the item rather than its list, so the change is applied
   * where it happened: re-sending a whole list around every checkbox would make
   * a long list feel slower the longer it got, which is the one thing a
   * checklist must not do.
   */
  function markCard(listId: string, itemId: string, done: boolean) {
    setBoards((items) =>
      items.map((board) =>
        board.id !== listId
          ? board
          : {
              ...board,
              columns: board.columns.map((column) => ({
                ...column,
                cards: column.cards.map((card) =>
                  card.id === itemId ? { ...card, done } : card,
                ),
              })),
            },
      ),
    );
  }

  /**
   * Optimistic, and rolled back if the server refuses.
   *
   * A checkbox is the one control a user expects to answer instantly, and a
   * controlled input that waits for a round trip visibly *un-ticks* itself
   * under the pointer before ticking again — which reads as a failed click, so
   * people click twice and untick what they just ticked. The rollback is what
   * keeps this honest: an optimistic tick that never gets corrected is a
   * checklist that lies about what the server thinks is done.
   */
  async function setTodoItemDone(list: Board, itemId: string, done: boolean) {
    markCard(list.id, itemId, done);
    try {
      const item = await api.setTodoItemDone(itemId, done);
      markCard(list.id, item.id, item.done);
    } catch (caught) {
      markCard(list.id, itemId, !done);
      setError(describeError(caught, "Could not update that item"));
    }
  }

  async function removeTodoItem(list: Board, itemId: string) {
    try {
      replaceBoard(await api.deleteBoardCard(list.id, itemId));
    } catch (caught) {
      setError(describeError(caught, "Could not delete that item"));
    }
  }

  async function removeTodoList(list: Board) {
    try {
      await api.deleteBoard(list.id);
      setBoards((items) => items.filter((item) => item.id !== list.id));
    } catch (caught) {
      setError(describeError(caught, "Could not delete that list"));
    }
  }

  return {
    todoOps: {
      createTodoList,
      addTodoItem,
      setTodoItemDone,
      removeTodoItem,
      removeTodoList,
    },
  };
}
