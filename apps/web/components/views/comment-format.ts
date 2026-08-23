import type { WorkspaceMember } from "@workspace/api-client";

/**
 * The pure half of commenting: what an @-token is, which members a draft
 * mentions, and how a stored body splits back into text and mention chips.
 *
 * Mentions travel as *names* in the body ("@Ada Lovelace") and as *ids* in the
 * request — the server only trusts the ids, and only keeps the ones that are
 * real members. Parsing is therefore done twice with the same rules: once at
 * send time to derive the ids from the final text (so a mention typed by hand
 * counts exactly like one picked from the completion list), and once at render
 * time to turn the kept names back into chips. One module, so the two passes
 * cannot drift.
 */

/** A member whose name could complete the token being typed, ranked like the
 * slash picker: name-prefix matches first, then word-prefix, then substring. */
export function matchMembers(
  members: WorkspaceMember[],
  query: string,
): WorkspaceMember[] {
  const needle = query.trim().toLowerCase();
  const ranked = members
    .map((member) => {
      const name = member.name.toLowerCase();
      let tier = -1;
      if (!needle) tier = 2;
      else if (name.startsWith(needle)) tier = 0;
      else if (name.split(/\s+/).some((word) => word.startsWith(needle))) tier = 1;
      else if (name.includes(needle)) tier = 2;
      return { member, tier };
    })
    .filter((row) => row.tier >= 0);
  ranked.sort(
    (a, b) =>
      a.tier - b.tier || a.member.name.localeCompare(b.member.name),
  );
  return ranked.map((row) => row.member).slice(0, 6);
}

/**
 * The @-token being typed at the end of the draft, or null when the caret is
 * not on one. "@" must open a word — "a@b" is an email, not a mention — and
 * the token runs to the end of the draft, which is where the completion list
 * makes sense. Returns the partial query, possibly "".
 */
export function mentionQuery(draft: string): string | null {
  const match = /(?:^|\s)@([^\s@]*(?: [^\s@]*)?)$/.exec(draft);
  return match ? match[1] : null;
}

/** Replace the trailing @-token with the picked member's name, plus the space
 * that closes the completion. */
export function completeMention(draft: string, name: string): string {
  return draft.replace(/@[^@]*$/, `@${name} `);
}

/** Every position-ordered occurrence of `@<member name>` in `body`, matched
 * longest-name-first so "@Ada Lovelace" can never be claimed by an "@Ada". */
function occurrences(
  body: string,
  members: WorkspaceMember[],
): Array<{ start: number; end: number; member: WorkspaceMember }> {
  const byLength = [...members]
    .filter((member) => member.name.trim().length > 0)
    .sort((a, b) => b.name.length - a.name.length);
  const found: Array<{ start: number; end: number; member: WorkspaceMember }> = [];
  const taken: Array<[number, number]> = [];
  const lower = body.toLowerCase();
  for (const member of byLength) {
    const token = `@${member.name.toLowerCase()}`;
    let from = 0;
    for (;;) {
      const start = lower.indexOf(token, from);
      if (start === -1) break;
      from = start + 1;
      const end = start + token.length;
      // "@" must open a word and the name must close one, or "x@ada" and
      // "@adam" would both count as mentioning Ada.
      const before = start === 0 ? " " : body[start - 1];
      const after = end >= body.length ? " " : body[end];
      if (/[\w@]/.test(before) || /\w/.test(after)) continue;
      if (taken.some(([s, e]) => start < e && end > s)) continue;
      taken.push([start, end]);
      found.push({ start, end, member });
    }
  }
  found.sort((a, b) => a.start - b.start);
  return found;
}

/** The member ids a finished draft mentions, in order of first appearance —
 * exactly what the create request should carry. */
export function parseMentions(
  body: string,
  members: WorkspaceMember[],
): string[] {
  const ids: string[] = [];
  for (const hit of occurrences(body, members)) {
    if (!ids.includes(hit.member.user_id)) ids.push(hit.member.user_id);
  }
  return ids;
}

export type CommentSegment =
  | { kind: "text"; text: string }
  | { kind: "mention"; text: string; user_id: string };

/**
 * A stored body split for rendering: plain text runs and mention chips. Only
 * names in `mentionIds` — the ids the server actually kept — become chips, so
 * a dropped foreign mention renders as the plain text it really was.
 */
export function splitMentions(
  body: string,
  mentionIds: string[],
  members: WorkspaceMember[],
): CommentSegment[] {
  const kept = new Set(mentionIds);
  const segments: CommentSegment[] = [];
  let cursor = 0;
  for (const hit of occurrences(body, members)) {
    if (!kept.has(hit.member.user_id)) continue;
    if (hit.start > cursor) {
      segments.push({ kind: "text", text: body.slice(cursor, hit.start) });
    }
    segments.push({
      kind: "mention",
      text: body.slice(hit.start, hit.end),
      user_id: hit.member.user_id,
    });
    cursor = hit.end;
  }
  if (cursor < body.length || segments.length === 0) {
    segments.push({ kind: "text", text: body.slice(cursor) });
  }
  return segments;
}
