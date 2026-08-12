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
