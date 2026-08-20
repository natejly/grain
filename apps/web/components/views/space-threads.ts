import type { Conversation, Source, Space } from "@workspace/api-client";

/**
 * Which of a space's things are on screen — pure, DOM-free, exhaustively
 * testable (see tests/space-threads.test.ts), in the folder-tree.ts mould.
 *
 * `spaceId === ""` deliberately matches NOTHING in the two filters below. ""
 * is the wire spelling of "no space", so a careless caller would otherwise
 * receive every unspaced thread in the workspace where it expected one
 * space's — the exact over-matching mistake the empty-string convention
 * invites, refused here once instead of at every call site.
 */
export function threadsInSpace(
  conversations: Conversation[],
  spaceId: string,
): Conversation[] {
  if (!spaceId) return [];
  return conversations.filter((conversation) => conversation.space_id === spaceId);
}

export function sourcesInSpace(sources: Source[], spaceId: string): Source[] {
  if (!spaceId) return [];
  return sources.filter((source) => source.space_id === spaceId);
}

/**
 * The name for a thread's space chip, or "" when no chip should render — an
 * unspaced thread, or a `space_id` whose space is gone or not yet loaded. ""
 * rather than a throw because this runs inside the rail's render: a deleted
 * space must degrade to an ordinary-looking thread, never a blank sidebar.
 */
export function spaceNameOf(conversation: Conversation, spaces: Space[]): string {
  if (!conversation.space_id) return "";
  return spaces.find((space) => space.id === conversation.space_id)?.name ?? "";
}
