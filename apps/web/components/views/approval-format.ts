import type { AgentToolCall, ApprovalMode } from "@workspace/api-client";

/**
 * The four approval modes, as a person has to understand them.
 *
 * The failure mode this whole surface is designed around is not switching the
 * bypass *on* — it is forgetting it is on. So every string here is written to
 * be read later, by someone who does not remember making the choice: each mode
 * says what will happen the next time the assistant wants to write, not what
 * the setting is called.
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
    mode: "auto_writes",
    label: "Allow all tool calls",
    detail:
      "The default. Every tool runs without stopping to ask. Denied tools stay denied.",
    bypass: true,
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
 * The unknown-mode fallback, looked up BY NAME rather than taken as
 * `APPROVAL_MODES[0]`.
 *
 * Position is not a safety property. Reordering that list for the picker is a
 * presentation decision, and it must never be able to turn this fallback into a
 * bypass — naming the entry keeps the strict answer strict wherever it sits.
 */
const STRICT_MODE: ApprovalModeInfo = APPROVAL_MODES.find(
  (item) => item.mode === "ask_writes",
)!;

/**
 * The mode's description, falling back to the strict one.
 *
 * A conversation stored with a mode this build has since dropped must read as
 * the *narrow* answer rather than as whatever string the column happens to
 * hold — an unrecognised value must never render as "no approvals needed".
 */
export function describeMode(mode: string): ApprovalModeInfo {
  return APPROVAL_MODES.find((item) => item.mode === mode) ?? STRICT_MODE;
}

export function isBypass(mode: string): boolean {
  return describeMode(mode).bypass;
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
