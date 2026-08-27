import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * Adding a todo item reports what went wrong, however it went wrong.
 *
 * The checklist's add box clears on submit and the item appears when the board
 * comes back. So when an add does nothing, the person sees their typed item
 * vanish with no row and no message — indistinguishable from the app having
 * missed the keystroke, which makes retyping it the reasonable next move.
 *
 * There are two ways an add can fail, and only one of them used to say so. The
 * request rejecting was reported; a list whose sole column could not be
 * resolved hit a bare `return` and was swallowed — the one silent exit in a
 * file where every other failure goes through `setError`. Both are pinned here
 * because the silent one is invisible in the UI by construction: nothing about
 * the screen distinguishes it from a dropped keypress.
 */
const addBoardCard = vi.fn();

vi.mock("../components/api", () => ({
  api: {
    addBoardCard: (...args: unknown[]) => addBoardCard(...args),
  },
}));

import type { Board } from "@workspace/api-client";
import { createTodoHandlers } from "../components/handlers/todos";

const LIST = {
  id: "list-1",
  name: "Launch checklist",
  columns: [{ id: "col-1", name: "To do", cards: [] }],
} as unknown as Board;

/** The same list after losing its column — a board that is no longer a list. */
const COLUMNLESS = { ...LIST, columns: [] } as unknown as Board;

function handlers() {
  const state = { error: "", boards: [LIST] };
  const { todoOps } = createTodoHandlers({
    setError: ((value: string) => {
      state.error = value;
    }) as never,
    setBoards: (() => undefined) as never,
  });
  return { made: todoOps, state };
}

beforeEach(() => {
  addBoardCard.mockReset();
});

describe("adding a todo item", () => {
  it("says so when the list has no column to add to", async () => {
    const { made, state } = handlers();

    await made.addTodoItem(COLUMNLESS, "Book the venue");

    // Nothing was sent — there is no column to send it to — and that is exactly
    // why the message has to come from here: no request means no rejection to
    // report, so a handler that only reports rejections reports nothing at all.
    expect(addBoardCard).not.toHaveBeenCalled();
    expect(state.error).not.toBe("");
    expect(state.error).toContain("Launch checklist");
  });

  it("says so when the request is refused", async () => {
    const { made, state } = handlers();
    addBoardCard.mockRejectedValue(new Error("nope"));

    await made.addTodoItem(LIST, "Book the venue");

    expect(addBoardCard).toHaveBeenCalledWith("list-1", "To do", "Book the venue");
    expect(state.error).not.toBe("");
  });

  it("leaves no stale error behind a later success", async () => {
    const { made, state } = handlers();
    addBoardCard.mockRejectedValue(new Error("nope"));
    await made.addTodoItem(LIST, "Book the venue");
    expect(state.error).not.toBe("");

    addBoardCard.mockResolvedValue(LIST);
    await made.addTodoItem(LIST, "Send the invites");

    // A message from the failed attempt still on screen under a row that did
    // land reads as the row having failed.
    expect(state.error).toBe("");
  });
});
