import type {
  AgentToolCall,
  ApprovalMode,
  WorkspaceMember,
} from "@workspace/api-client";

/**
 * The five approval modes, as a person has to understand them.
 *
 * Every string here is written to be read later, by someone who does not
 * remember making the choice: each mode says what will happen the next time the
 * assistant wants to write, not what the setting is called.
 *
 * Ordered loosest-first because the first entry is the default a new thread
 * arrives in, and a list whose default sits in the middle reads as though the
 * top one is. `bypass` still marks the two modes that let a write through
 * unreviewed — that flag decides what the trail SAYS, not how alarmed it is;
 * `BypassIndicator`'s `tone` is what decides that, and it is calm for a member
 * whose default this is.
 */
export type ApprovalModeInfo = {
  mode: ApprovalMode;
  /** On the control, and in the indicator. Short enough to sit in a button. */
  label: string;
  /** What it does, in the picker. */
  detail: string;
  /** True for the one mode that lets a write through unreviewed. */
  bypass: boolean;
};

export const APPROVAL_MODES: ApprovalModeInfo[] = [
  {
    mode: "auto_writes",
    label: "Act on its own",
    detail:
      "The default. Writes go through and show up in the trail. Denied tools stay denied, and a flagged turn still asks.",
    bypass: true,
  },
  {
    mode: "ask_writes",
    label: "Ask before writes",
    detail: "Searches run on their own; anything that changes something waits for you.",
    bypass: false,
  },
  {
    mode: "ask_all",
    label: "Ask before everything",
    detail: "Every tool waits, searches included.",
    bypass: false,
  },
  {
    mode: "plan",
    label: "Plan first",
    detail:
      "Research only — nothing changes until you approve the plan it proposes.",
    bypass: false,
  },
  {
    mode: "guardian",
    label: "Guardian auto-approve",
    detail:
      "A reviewer model approves routine writes; anything surprising still waits for you.",
    // A bypass in the honest sense the trail tracks: a write can run without a
    // person seeing it first. The reviewer narrows how often, not whether.
    bypass: true,
  },
];

/**
 * The mode's description, falling back to the strict one.
 *
 * A conversation stored with a mode this build has since dropped must read as
 * the *narrow* answer rather than as whatever string the column happens to
 * hold — an unrecognised value must never render as "no approvals needed".
 */
/**
 * The mode's copy, with an unknown value landing on the strict answer.
 *
 * The fallback is looked up BY NAME, never `APPROVAL_MODES[0]`. The list is
 * ordered for the picker, and it was reordered the day the default became
 * `auto_writes` — with an index fallback, that edit alone would have made every
 * unrecognised mode render, and answer `isBypass`, as a bypass. A display
 * default has to be the strict one on purpose, not by position.
 */
export function describeMode(mode: string): ApprovalModeInfo {
  const known = APPROVAL_MODES.find((item) => item.mode === mode);
  if (known) return known;
  const strict = APPROVAL_MODES.find((item) => item.mode === "ask_writes");
  // Non-null in practice; the literal keeps this total even if the entry is
  // ever renamed out from under it, and keeps `bypass` false either way.
  return (
    strict ?? {
      mode: "ask_writes",
      label: "Ask before writes",
      detail: "Anything that changes something waits for you.",
      bypass: false,
    }
  );
}

export function isBypass(mode: string): boolean {
  return describeMode(mode).bypass;
}

/**
 * The provenance classes, as a sentence names them. Machine name in, the thing
 * a reader would call it out.
 */
const PROVENANCE_WORDS: Record<string, string> = {
  web_fetch: "a page fetched from the web",
  mcp_result: "a result from a connected MCP server",
  sandbox_output: "output from the sandbox",
  workspace_chunk: "a passage from your library",
  memory_item: "a saved memory",
  tool_result: "a tool's output",
  user_direct: "something you typed",
};

/** What the waiting call would do. Two risks, two words. */
const ACTION_WORDS: Record<string, string> = {
  write: "changes something",
  egress: "reaches the network",
};

function joinWords(words: string[]): string {
  if (words.length === 1) return words[0];
  if (words.length === 2) return `${words[0]} and ${words[1]}`;
  return `${words.slice(0, -1).join(", ")} and ${words[words.length - 1]}`;
}

/**
 * Why this call is waiting, in a sentence — or "" when we cannot say.
 *
 * Parses exactly `taint:<comma,classes>[,+N]:<action>`. Any other shape, a
 * class this build does not know, or an unknown action returns "", never a
 * guess: the same doctrine `describeMode` follows for an unrecognised mode,
 * and the thing that keeps a future server string from rendering as garbage
 * inside an approval card.
 *
 * `+N` is the server saying it had to drop N class names to fit the column's
 * 64 characters (`provenance.gate_reason`). It is rendered, never ignored:
 * the names sort alphabetically, so the ones that fall off the end are
 * `web_fetch` and `workspace_chunk` — and a card that listed two of three
 * sources and read as a complete sentence would understate what the turn
 * read, which is the one direction this copy must not be wrong in.
 *
 * The wording is deliberate and the limit is real. It says "This turn read …",
 * never "This block triggered …". Nothing in a model's output proves which
 * piece of context caused which tool call — a model can act on something it
 * read three steps ago — and the enforced guarantee is strictly turn-level. A
 * reviewer who read this card as proof of causation would have been misled by
 * us, so the sentence does not offer that reading.
 */
export function describeGate(reason: string): string {
  const parts = (reason || "").split(":");
  if (parts.length !== 3 || parts[0] !== "taint") return "";
  const action = ACTION_WORDS[parts[2]];
  if (!action) return "";
  const classes = parts[1].split(",").filter(Boolean);
  if (classes.length === 0) return "";
  const words: string[] = [];
  let elided = 0;
  for (const name of classes) {
    const dropped = /^\+([1-9][0-9]*)$/.exec(name);
    if (dropped) {
      elided = Number(dropped[1]);
      continue;
    }
    const word = PROVENANCE_WORDS[name];
    // One unknown class poisons the whole sentence rather than being dropped:
    // a reason that silently listed two of three sources would understate what
    // the turn read, which is the one direction this copy must not be wrong in.
    if (!word) return "";
    if (!words.includes(word)) words.push(word);
  }
  if (words.length === 0) return "";
  if (elided > 0) {
    words.push(
      elided === 1 ? "one other kind of source" : `${elided} other kinds of source`,
    );
  }
  return (
    `This turn read ${joinWords(words)}. Because this call ${action}, it is ` +
    "waiting for you even though the thread is set to act on its own."
  );
}

/**
 * The calls this thread's bypass let through, newest last.
 *
 * Read off `approved_by_mode`, which the server sets only where the mode
 * actually changed the answer — a tool a standing policy already allowed is not
 * in here, because the bypass did not decide it. That is the difference between
 * a trail and a guess: inferring "the mode was on and this was a write" would
 * credit the bypass with calls it had no part in.
 */
export function autoApprovedCalls(
  calls: AgentToolCall[],
  conversationId: string | null,
): AgentToolCall[] {
  if (!conversationId) return [];
  return calls.filter(
    (call) => call.conversation_id === conversationId && call.approved_by_mode !== "",
  );
}

/** "run_python and 2 more", for the indicator's one-line summary. */
export function summariseAutoApproved(calls: AgentToolCall[]): string {
  const names = [...new Set(calls.map((call) => call.name))];
  if (names.length === 0) return "Nothing yet";
  if (names.length === 1) return names[0];
  return `${names[0]} and ${names.length - 1} more`;
}

/** Anything routable: the feed's approval rows and the shell's call rows both
 * carry `assigned_to` ("" = anyone), which is all partitioning reads. */
type Assignable = { assigned_to: string };

/**
 * The queue split by who each row waits on, order preserved within each group.
 *
 * The server deliberately never hides assigned-away approvals — nothing parked
 * is invisible — so the split happens here: "yours" and "unassigned" are the
 * actionable queue, "others" renders de-emphasized. Before the identity's
 * first read lands (`selfId === ""`), nothing is claimed as yours; assigned
 * rows fall to "others" rather than to a guess.
 */
export function partitionApprovals<T extends Assignable>(
  rows: T[],
  selfId: string,
): { mine: T[]; unassigned: T[]; others: T[] } {
  const mine: T[] = [];
  const unassigned: T[] = [];
  const others: T[] = [];
  for (const row of rows) {
    if (!row.assigned_to) unassigned.push(row);
    else if (selfId && row.assigned_to === selfId) mine.push(row);
    else others.push(row);
  }
  return { mine, unassigned, others };
}

/**
 * The rows the caller can actually answer — theirs first, then anyone's —
 * which is what the rail badge counts and the waiting strip previews. A row
 * routed to a colleague is their wait, not this caller's, so it is neither
 * counted at the rail nor put behind a decide button that would 409.
 */
export function actionableApprovals<T extends Assignable>(
  rows: T[],
  selfId: string,
): T[] {
  const { mine, unassigned } = partitionApprovals(rows, selfId);
  return [...mine, ...unassigned];
}

/** A member's display name for the assignee control, falling back to the id
 * so a departed member's assignment still reads as *somebody* specific. */
export function assigneeName(userId: string, members: WorkspaceMember[]): string {
  if (!userId) return "Anyone";
  const member = members.find((item) => item.user_id === userId);
  return member ? member.name : userId;
}
