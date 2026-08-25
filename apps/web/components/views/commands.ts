/**
 * The composer's built-in slash commands, beside the user-authored skills the
 * same "/" summons.
 *
 * Commands are frontend-resolved, exactly as skills are: picking one performs a
 * structured action (a mode change, an aside send) and nothing command-shaped
 * ever travels to the server as message text — the demo-era `/tool` prefix is
 * the cautionary tale. The one parse that happens at send time (`parseAside`)
 * still resolves to a structured field on the wire, never to a server-side
 * string match.
 */

export type BuiltinCommand = {
  name: "plan" | "btw";
  /** What picking it does, in the picker. */
  description: string;
};

export const BUILTIN_COMMANDS: BuiltinCommand[] = [
  {
    name: "plan",
    description:
      "Plan first: research and propose a plan you approve before anything changes.",
  },
  {
    name: "btw",
    description: "Leave a note for the next turn without starting one.",
  },
];

/**
 * Commands whose name starts with the text typed after the "/".
 *
 * Prefix-match on the bare name, and only before the first space — a draft
 * that has moved past the token ("/btw remember the deadline") is being
 * written, not searched, and must not keep the picker open over it. Looser
 * matching than skills on purpose: two fixed names earn no fuzzy search.
 */
export function matchCommands(query: string): BuiltinCommand[] {
  const token = query.toLowerCase();
  if (/\s/.test(token)) return [];
  return BUILTIN_COMMANDS.filter((command) => command.name.startsWith(token));
}

/**
 * The picker line for a command, given the thread's current approval mode —
 * /plan is a toggle, and a toggle must say which way it is about to flip.
 */
export function commandDescription(
  command: BuiltinCommand,
  approvalMode: string | null,
): string {
  if (command.name === "plan" && approvalMode === "plan") {
    return "Turn plan mode off — the thread goes back to asking before writes.";
  }
  return command.description;
}

/**
 * The aside text of a "/btw" draft, or null when the draft is not one.
 *
 * "" (a bare "/btw") is distinct from null: the draft *is* an aside, there is
 * just nothing in it yet, and the composer refuses the send rather than
 * recording an empty note. "/btwsomething" is null — a word that merely starts
 * with the token is not the command.
 */
export function parseAside(draft: string): string | null {
  const match = /^\/btw(?:\s+([\s\S]*))?$/i.exec(draft.trim());
  if (!match) return null;
  return (match[1] ?? "").trim();
}
