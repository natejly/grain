// @vitest-environment jsdom
import { cleanup, render } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Board } from "@workspace/api-client";
import type { CoworkingState } from "../components/use-coworking";
import { BoardView, type BoardViewProps } from "../components/views/board";

/**
 * The pointer layer over boards: one layer PER BOARD, addressed as
 * `board:<id>`, so two people on different boards never see each other's
 * cursors — and none of it exists when the coworking channel is not wired,
 * because the page must render identically for a solo workspace.
 */

function board(id: string, columns: number): Board {
  return {
    id,
    name: `Board ${id}`,
    columns: Array.from({ length: columns }, (_, i) => ({
      id: `${id}-c${i}`,
      name: `Column ${i}`,
      cards: [],
    })),
  };
}

const noop = async () => undefined;

function coworking(): CoworkingState {
  return {
    runs: [],
    presences: [],
    othersOn: vi.fn(() => []),
    report: () => undefined,
    reportPointer: () => undefined,
    leave: () => undefined,
  };
}

function view(overrides: Partial<BoardViewProps> = {}) {
  return createElement(BoardView, {
    boards: [board("b1", 3), board("b2", 3)],
    createBoard: noop,
    addCard: noop,
    moveCard: noop,
    removeCard: noop,
    removeBoard: noop,
    todoOps: {
      createTodoList: noop,
      addTodoItem: noop,
      setTodoItemDone: noop,
      removeTodoItem: noop,
      removeTodoList: noop,
      claimTodoItem: noop,
      releaseTodoItem: noop,
    },
    ...overrides,
  });
}

afterEach(cleanup);

describe("the board pointer layers", () => {
  it("mounts one layer per board, each on its own surface", () => {
    const channel = coworking();
    const { container } = render(view({ coworking: channel }));

    expect(container.querySelectorAll(".live-cursor-layer.board-cursor-box")).toHaveLength(2);
    // The address is the invariant: per board, never per page.
    expect(channel.othersOn).toHaveBeenCalledWith("board:b1");
    expect(channel.othersOn).toHaveBeenCalledWith("board:b2");
    expect(channel.othersOn).not.toHaveBeenCalledWith("board:b1,b2");
  });

  it("wraps a one-column list the same way — a list IS a board", () => {
    const channel = coworking();
    const { container } = render(
      view({ boards: [board("b3", 1)], coworking: channel }),
    );
    expect(container.querySelectorAll(".live-cursor-layer.board-cursor-box")).toHaveLength(1);
    expect(channel.othersOn).toHaveBeenCalledWith("board:b3");
  });

  it("renders without cursors or beats when coworking is off", () => {
    const { container } = render(view());
    // The wrapper div is still there (the layer renders inertly without the
    // channel), but no cursor is ever drawn and no pointer is reported.
    expect(container.querySelector(".live-cursor")).toBeNull();
  });
});
