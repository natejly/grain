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
const deleteBoardCard = vi.fn();
const setTodoItemDone = vi.fn();
const deleteBoard = vi.fn();

vi.mock("../components/api", () => ({
  api: {
    addBoardCard: (...args: unknown[]) => addBoardCard(...args),
    deleteBoardCard: (...args: unknown[]) => deleteBoardCard(...args),
    setTodoItemDone: (...args: unknown[]) => setTodoItemDone(...args),
    deleteBoard: (...args: unknown[]) => deleteBoard(...args),
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

type TodoOpsUnderTest = ReturnType<typeof handlers>["made"];

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
  deleteBoardCard.mockReset();
  setTodoItemDone.mockReset();
  deleteBoard.mockReset();
});

/** Leave an unread message on screen, the way a failed add does. */
async function withStaleError(made: TodoOpsUnderTest, state: { error: string }) {
  addBoardCard.mockRejectedValue(new Error("nope"));
  await made.addTodoItem(LIST, "Book the venue");
  expect(state.error).not.toBe("");
}

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

/**
 * Which operations clear the error on entry, and the one that must not.
 *
 * Boards render as a row of lists sharing a single error line, so a message
 * left over from a failed operation sits under whatever the person does next
 * and reads as THAT having failed. Every discrete, deliberate operation
 * therefore clears on entry.
 *
 * Ticking a checkbox is the exception, and it is pinned here rather than left
 * to look like the omission it resembles: the tick is optimistic, fires on
 * every item in the list, and is the thing someone does while reading an error
 * about something else. Clearing there would wipe a message before it was read.
 */
describe("the shared error line", () => {
  it("is cleared when a delete is started", async () => {
    const { made, state } = handlers();
    await withStaleError(made, state);

    deleteBoardCard.mockResolvedValue(LIST);
    await made.removeTodoItem(LIST, "item-1");

    expect(state.error).toBe("");
  });

  it("is cleared when a list delete is started", async () => {
    const { made, state } = handlers();
    await withStaleError(made, state);

    deleteBoard.mockResolvedValue(undefined);
    await made.removeTodoList(LIST);

    expect(state.error).toBe("");
  });

  it("survives a checkbox tick, which is not aimed at it", async () => {
    const { made, state } = handlers();
    await withStaleError(made, state);

    setTodoItemDone.mockResolvedValue({ id: "item-1", done: true });
    await made.setTodoItemDone(LIST, "item-1", true);

    // Deliberate. A tick is incidental to the message on screen, so it leaves
    // it for an operation the person actually aimed at. Do not "fix" this.
    expect(state.error).not.toBe("");
  });
});
