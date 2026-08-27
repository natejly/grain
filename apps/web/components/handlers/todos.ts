"use client";

import type { Board, BoardCard, TodoItem } from "@workspace/api-client";
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
    // Cleared on entry, like every other handler that reports: boards render as
    // a row of lists sharing one error line, so a message left over from a
    // failed add sits under a row that later landed and reads as that row
    // having failed.
    setError("");
    const column = soleColumn(list);
    if (!column) {
      // A list is a board with exactly one column, so an empty name here is a
      // shape that should never reach the UI. Returning in silence made that
      // case indistinguishable from a slow add — nothing appeared, nothing
      // said why. Every other failure in this file is shown; so is this one.
      //
      // Named, not "that list": several lists are on screen at once and they
      // share the one error line, so an unnamed message does not say which.
      setError(`Could not add that item: “${list.name}” has no column.`);
      return;
    }
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
  function patchCard(listId: string, itemId: string, patch: Partial<BoardCard>) {
    setBoards((items) =>
      items.map((board) =>
        board.id !== listId
          ? board
          : {
              ...board,
              columns: board.columns.map((column) => ({
                ...column,
                cards: column.cards.map((card) =>
                  card.id === itemId ? { ...card, ...patch } : card,
                ),
              })),
            },
      ),
    );
  }

  function markCard(listId: string, itemId: string, done: boolean) {
    patchCard(listId, itemId, { done });
  }

  /** The claim fields of an item response, as a card patch. */
  function claimOf(item: TodoItem): Partial<BoardCard> {
    return {
      claimed: item.claimed ?? false,
      claimed_by: item.claimed_by ?? "",
      claimed_kind: item.claimed_kind ?? "",
      claimed_label: item.claimed_label ?? "",
      claimed_run_id: item.claimed_run_id ?? "",
      claim_expires_at: item.claim_expires_at ?? null,
    };
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
      // The confirm also carries the claim: ticking releases it server-side,
      // so the "who is on this" chip clears with the same response.
      patchCard(list.id, item.id, { done: item.done, ...claimOf(item) });
    } catch (caught) {
      markCard(list.id, itemId, !done);
      setError(describeError(caught, "Could not update that item"));
    }
  }

  /**
   * Not optimistic, unlike the tick: a claim is a race the server referees,
   * and showing "yours" before the referee answers is how two workers both
   * believe they won the same card.
   */
  async function claimTodoItem(list: Board, itemId: string) {
    setError("");
    try {
      patchCard(list.id, itemId, claimOf(await api.claimTodoItem(itemId)));
    } catch (caught) {
      setError(describeError(caught, "Could not claim that item"));
    }
  }

  /** `force` is the human "take over" — it frees an agent's card too. */
  async function releaseTodoItem(list: Board, itemId: string, force = false) {
    setError("");
    try {
      patchCard(list.id, itemId, claimOf(await api.releaseTodoItem(itemId, force)));
    } catch (caught) {
      setError(describeError(caught, "Could not release that item"));
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
      claimTodoItem,
      releaseTodoItem,
      removeTodoItem,
      removeTodoList,
    },
  };
}
