import type {
  Conversation,
  ProjectSummary,
  Source,
  Space,
} from "@workspace/api-client";

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

/** One rail group: a space and the visible threads filed in it. */
export type SpaceThreadGroup = {
  space: Space;
  threads: Conversation[];
};

/**
 * The rail's space groups: every space that holds at least one visible
 * thread, in the spaces list's own (name) order, each with its threads in the
 * server's recency order. A space with no visible threads gets no rail group
 * — its home is the Spaces page, and an empty header would be sidebar noise.
 * A thread whose `space_id` names a space the caller's list does not hold
 * (deleted between fetches) simply stays in the flat rail via the
 * complementary `unspacedThreads` — nothing disappears.
 */
export function spaceThreadGroups(
  spaces: Space[],
  conversations: Conversation[],
): SpaceThreadGroup[] {
  return spaces
    .map((space) => ({
      space,
      threads: conversations.filter(
        (conversation) => conversation.space_id === space.id,
      ),
    }))
    .filter((group) => group.threads.length > 0);
}

/**
 * The complement of `spaceThreadGroups` over the same list: what the flat
 * Personal/Shared rail still shows. A thread pointing at a space that is not
 * in `spaces` counts as unspaced here, so the two functions partition the
 * list between them whatever state the fetches are in.
 */
export function unspacedThreads(
  spaces: Space[],
  conversations: Conversation[],
): Conversation[] {
  const known = new Set(spaces.map((space) => space.id));
  return conversations.filter(
    (conversation) =>
      !conversation.space_id || !known.has(conversation.space_id),
  );
}

/** One rail group: a project and the threads that hang off it. */
export type ProjectThreadGroup = {
  project: ProjectSummary;
  threads: Conversation[];
};

/**
 * The project a thread is about, or "" when it is about no project.
 *
 * The KIND is checked first and is not optional: `subject_id` is a bare uuid
 * shared across three independent tables, so a dashboard thread whose id
 * happened to equal a project's would otherwise group under that project —
 * the same collision `conversations.for_subject` keys on the pair to avoid.
 */
function projectIdOf(conversation: Conversation): string {
  return conversation.subject_kind === "project" ? conversation.subject_id : "";
}

/**
 * The rail's project groups: every project that holds at least one visible
 * thread, in the projects list's own order, each with its threads in the
 * server's recency order. Empty groups are dropped for the same reason a
 * space's are — the project's home is the Projects page, and a header with
 * nothing under it is sidebar noise.
 *
 * A project whose id is "" (and a thread whose `subject_id` is "") matches
 * nothing, the same refusal `threadsInSpace` makes: "" is the wire spelling of
 * "no subject", and letting it match would file every ordinary rail thread
 * under the first project in the list.
 */
export function projectThreadGroups(
  projects: ProjectSummary[],
  conversations: Conversation[],
): ProjectThreadGroup[] {
  return projects
    .map((project) => ({
      project,
      threads: project.id
        ? conversations.filter(
            (conversation) => projectIdOf(conversation) === project.id,
          )
        : [],
    }))
    .filter((group) => group.threads.length > 0);
}

/**
 * What the flat Personal/Shared rail still shows once the space groups AND the
 * project groups have taken theirs: `unspacedThreads` minus every thread that
 * belongs to a project the caller's list holds.
 *
 * A thread whose project is not in `projects` (deleted between fetches, or the
 * list not yet loaded) stays here, so the three renderings partition the list
 * between them whatever state the fetches are in and no row can vanish.
 */
export function unfiledThreads(
  spaces: Space[],
  projects: ProjectSummary[],
  conversations: Conversation[],
): Conversation[] {
  const known = new Set(projects.map((project) => project.id).filter(Boolean));
  return unspacedThreads(spaces, conversations).filter(
    (conversation) => !known.has(projectIdOf(conversation)),
  );
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

/**
 * The same contract for anything that carries a bare `space_id` — source rows
 * and memory rows wear the chip too, and they hold the id without a
 * Conversation around it. "" degrades identically: no chip, never a blank row.
 */
export function spaceNameForId(spaceId: string, spaces: Space[]): string {
  if (!spaceId) return "";
  return spaces.find((space) => space.id === spaceId)?.name ?? "";
}
