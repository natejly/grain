import type { AgentToolCall, Board, BoardCard } from "@workspace/api-client";

/**
 * Reading a board as a todo list.
 *
 * A list is **a board with exactly one column** — the server derives it that
 * way rather than storing a flag, so the client derives it the same way rather
 * than asking a second endpoint what the first one already said. `GET
 * /api/boards` carries `done` on every card, which means the shell's existing
 * board state is already everything a checklist needs: no second fetch, no
 * second slice of state to keep in step, and a list the agent just created
 * appears wherever boards do.
 *
 * The consequence worth knowing: deleting two columns off a kanban turns it
 * into a list here, exactly as it does on the server. That is the right answer
 * — a board with one column has nothing to drag between and everything to tick
 * off — and it is the whole reason the rule is stated once instead of twice.
 */
export function isTodoList(board: Board): boolean {
  return board.columns.length === 1;
}

export function todoListsFrom(boards: Board[]): Board[] {
  return boards.filter(isTodoList);
}

/**
 * Which glyph a board wears in the one merged listing. A discriminant rather
 * than an icon component, so this file stays free of React and the rule stays
 * testable beside the predicate it restates: crossing one column flips the
 * glyph, and *only* the glyph — the object itself never moves.
 */
export function glyphFor(board: Board): "list" | "board" {
  return isTodoList(board) ? "list" : "board";
}

/**
 * The toast lines earned by a boards refresh, given the shapes the previous
 * refresh had.
 *
 * This is the merge's contract made visible: the object stays put when its
 * column count crosses 1, so *something* has to say the presentation changed
 * or the checkboxes silently become columns. Diffing state is the only seam
 * every mutation path shares — column ops patch a Board back, list creation
 * refetches, agent tools replace the array wholesale.
 *
 * An id absent from `previous` says nothing changed — it is a first load or a
 * creation, and a thing being seen for the first time has not graduated.
 */
export function graduationNotices(
  previous: Map<string, boolean>,
  boards: Board[],
): string[] {
  const notices: string[] = [];
  for (const board of boards) {
    const wasList = previous.get(board.id);
    if (wasList === undefined || wasList === isTodoList(board)) continue;
    notices.push(isTodoList(board) ? "Now showing as a list" : "Now showing as a board");
  }
  return notices;
}

/**
 * The one toast line for a refresh, "" when nothing graduated. There is one
 * toast slot, so several flips in one refresh — a workflow reshaping two
 * boards at once — share the line rather than the last flip silently winning.
 */
export function graduationNotice(
  previous: Map<string, boolean>,
  boards: Board[],
): string {
  return graduationNotices(previous, boards).join(" · ");
}

/** A list's items, in board order. Its sole column is the list. */
export function itemsOf(board: Board): BoardCard[] {
  return board.columns[0]?.cards ?? [];
}

export function openCount(board: Board): number {
  return itemsOf(board).filter((card) => !card.done).length;
}

/** "2 of 5 done", or "Nothing on this list yet" while it is empty. */
export function describeProgress(board: Board): string {
  const items = itemsOf(board);
  if (items.length === 0) return "Nothing on this list yet";
  return `${items.length - openCount(board)} of ${items.length} done`;
}

function argumentsOf(call: AgentToolCall): Record<string, unknown> {
  try {
    const parsed: unknown = JSON.parse(call.arguments_json || "{}");
    return parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

function text(args: Record<string, unknown>, key: string): string {
  const value = args[key];
  return typeof value === "string" ? value.trim() : "";
}

/** Tool calls that leave a checklist worth showing in the conversation. */
export const TODO_TOOLS = ["add_todo", "todo_check"];

/**
 * Which list a todo tool call was about, or null when it cannot be known.
 *
 * The same rule `services/artifacts/todos.resolve_list` applies, in the same
 * order: an explicit `list_id`, then an explicit `list` by name, then — when
 * the workspace has exactly one list — that one, because the common case is one
 * list and neither the model nor this function should have to name it.
 *
 * Null where the server itself would refuse to guess. A checklist drawn against
 * the wrong list is worse than no checklist: it would show a user three items
 * ticked off that nobody ticked, in a conversation that claims the assistant
 * did it.
 */
export function listForTodoCall(call: AgentToolCall, lists: Board[]): Board | null {
  if (!TODO_TOOLS.includes(call.name)) return null;
  const args = argumentsOf(call);
  const byId = text(args, "list_id");
  if (byId) return lists.find((board) => board.id === byId) ?? null;
  const byName = text(args, "list").toLowerCase();
  if (byName) {
    return lists.find((board) => board.name.trim().toLowerCase() === byName) ?? null;
  }
  return lists.length === 1 ? lists[0] : null;
}
