import type { AgentToolCall, Board } from "@workspace/api-client";
import { describe, expect, it } from "vitest";
import {
  describeProgress,
  glyphFor,
  graduationNotice,
  graduationNotices,
  isTodoList,
  itemsOf,
  listForTodoCall,
  openCount,
  todoListsFrom,
} from "../components/views/todo-format";

/**
 * A todo list is a board with exactly one column — the server's rule, applied
 * client-side so the checklist can be drawn from board state the shell already
 * holds. These pin the two claims that rest on it: the derivation itself, and
 * the refusal to guess *which* list a tool call was about when the answer is
 * not knowable. A checklist drawn against the wrong list would show items
 * ticked that nobody ticked, in a conversation crediting the assistant.
 */
function card(id: string, title: string, done = false) {
  return { id, title, body: "", labels: [], done };
}

function board(id: string, name: string, columns: string[], cards = [card("c1", "Item")]) {
  return {
    id,
    name,
    columns: columns.map((column, index) => ({
      id: `${id}-col-${index}`,
      name: column,
      cards: index === 0 ? cards : [],
    })),
  } as Board;
}

function call(name: string, args: Record<string, unknown>): AgentToolCall {
  return {
    id: "call-1",
    run_id: "run-1",
    conversation_id: "conv-1",
    name,
    arguments_json: JSON.stringify(args),
    proposal_preview: "",
    status: "succeeded",
    result_preview: "",
    error: "",
    latency_ms: 0,
    artifacts: [],
    approved_by_mode: "",
    created_at: "2026-01-01T00:00:00Z",
  };
}

describe("a list is a board with one column", () => {
  it("reads a one-column board as a list and a three-column board as a board", () => {
    expect(isTodoList(board("b1", "Groceries", ["To do"]))).toBe(true);
    expect(isTodoList(board("b2", "Sprint", ["Todo", "Doing", "Done"]))).toBe(false);
  });

  it("flips only the glyph at the threshold — the object never leaves the listing", () => {
    // The merged page renders ALL boards in one stable order; crossing one
    // column changes what an entry wears, not where it sits. `todoListsFrom`
    // is still the filter chat draws its checklists from.
    const groceries = board("b1", "Groceries", ["To do"]);
    expect(glyphFor(groceries)).toBe("list");
    expect(glyphFor(board("b1", "Groceries", ["To do", "Doing"]))).toBe("board");
    const all = [groceries, board("b2", "Sprint", ["A", "B"])];
    expect(todoListsFrom(all).map((item) => item.id)).toEqual(["b1"]);
  });

  it("takes a list's items from its sole column", () => {
    const list = board("b1", "Groceries", ["To do"], [card("c1", "Milk"), card("c2", "Eggs", true)]);
    expect(itemsOf(list).map((item) => item.title)).toEqual(["Milk", "Eggs"]);
    expect(openCount(list)).toBe(1);
    expect(describeProgress(list)).toBe("1 of 2 done");
  });

  it("says a list is empty rather than reporting 0 of 0", () => {
    expect(describeProgress(board("b1", "Groceries", ["To do"], []))).toBe(
      "Nothing on this list yet",
    );
  });
});

describe("the graduation notice", () => {
  const shapes = (boards: Board[]) =>
    new Map(boards.map((item) => [item.id, isTodoList(item)]));

  it("announces a list growing into a board, and a board shrinking into a list", () => {
    const before = shapes([board("b1", "Groceries", ["To do"])]);
    expect(graduationNotices(before, [board("b1", "Groceries", ["To do", "Doing"])])).toEqual([
      "Now showing as a board",
    ]);
    const sprint = shapes([board("b2", "Sprint", ["A", "B", "C"])]);
    expect(graduationNotices(sprint, [board("b2", "Sprint", ["A"])])).toEqual([
      "Now showing as a list",
    ]);
  });

  it("says nothing while nothing crosses the threshold", () => {
    const all = [board("b1", "Groceries", ["To do"]), board("b2", "Sprint", ["A", "B"])];
    expect(graduationNotices(shapes(all), all)).toEqual([]);
    // Growing a three-column board to four is not a graduation.
    const grown = shapes([board("b2", "Sprint", ["A", "B", "C"])]);
    expect(graduationNotices(grown, [board("b2", "Sprint", ["A", "B", "C", "D"])])).toEqual([]);
  });

  it("ignores ids the previous refresh never saw — first load and creations", () => {
    expect(graduationNotices(new Map(), [board("b1", "Groceries", ["To do"])])).toEqual([]);
    const before = shapes([board("b1", "Groceries", ["To do"])]);
    expect(
      graduationNotices(before, [
        board("b1", "Groceries", ["To do"]),
        board("b3", "New board", ["A", "B", "C"]),
      ]),
    ).toEqual([]);
  });

  it("joins several flips into the one toast line rather than keeping only the last", () => {
    // One toast slot: a refresh that reshapes two boards at once — a workflow
    // can — must say so about both.
    const before = shapes([
      board("b1", "Groceries", ["To do"]),
      board("b2", "Sprint", ["A", "B", "C"]),
    ]);
    const after = [
      board("b1", "Groceries", ["To do", "Doing"]),
      board("b2", "Sprint", ["A"]),
    ];
    expect(graduationNotice(before, after)).toBe(
      "Now showing as a board · Now showing as a list",
    );
    // And "" — not a toast — while nothing crossed the threshold.
    expect(graduationNotice(shapes(after), after)).toBe("");
  });
});

describe("resolving the list a tool call was about", () => {
  const groceries = board("b1", "Groceries", ["To do"]);
  const chores = board("b2", "Chores", ["To do"]);

  it("prefers an explicit id, then an explicit name", () => {
    expect(listForTodoCall(call("add_todo", { list_id: "b2" }), [groceries, chores])?.id).toBe(
      "b2",
    );
    expect(
      listForTodoCall(call("todo_check", { list: "  groceries " }), [groceries, chores])?.id,
    ).toBe("b1");
  });

  it("falls back to the sole list, the courtesy the server extends too", () => {
    expect(listForTodoCall(call("add_todo", { title: "Milk" }), [groceries])?.id).toBe("b1");
  });

  it("refuses to guess where the server itself would refuse", () => {
    // Two lists and neither named: `todos.resolve_list` raises here, so drawing
    // a checklist would be inventing an answer the API declined to give.
    expect(listForTodoCall(call("add_todo", { title: "Milk" }), [groceries, chores])).toBeNull();
    // A named list that does not exist is not silently swapped for another.
    expect(listForTodoCall(call("add_todo", { list: "Nope" }), [groceries, chores])).toBeNull();
    expect(listForTodoCall(call("add_todo", { list_id: "gone" }), [groceries])).toBeNull();
  });

  it("shows nothing for a tool that is not about todos", () => {
    expect(listForTodoCall(call("create_document", {}), [groceries])).toBeNull();
  });

  it("survives arguments that are not an object", () => {
    const broken = { ...call("add_todo", {}), arguments_json: "not json" };
    expect(listForTodoCall(broken, [groceries])?.id).toBe("b1");
  });
});
