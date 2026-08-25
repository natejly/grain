import {
  ApiError,
  type Conversation,
  type DocumentKind,
  type Message,
  type Source,
} from "@workspace/api-client";

export type View =
  | "chat"
  | "agents"
  | "skills"
  | "sources"
  | "memory"
  | "graph"
  | "dashboards"
  | "apps"
  | "datasets"
  | "integrations"
  | "documents"
  | "boards"
  | "data"
  | "projects"
  | "mcp"
  | "sandbox-tools"
  | "activity"
  | "policies"
  | "admin"
  | "workflows"
  | "crons"
  | "spaces";

/**
 * An unreachable API already has a dedicated banner with a retry, so it returns
 * "" here rather than also raising a toast full of "Failed to fetch".
 */
export function describeError(caught: unknown, fallback: string): string {
  if (caught instanceof ApiError && caught.offline) return "";
  if (caught instanceof Error && caught.message) return caught.message;
  return fallback;
}

export function slugify(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 72);
}

export function baseName(filename: string): string {
  return filename.replace(/\.[^.]+$/, "") || filename;
}

export function isTabular(filename: string): boolean {
  return /\.(csv|json)$/i.test(filename);
}

/**
 * What each document format is called on screen, in the order they are offered.
 *
 * Two formats, and they differ in what the reader sees: markdown is rendered
 * (headings, lists, and $…$ maths through KaTeX), plain text is not touched at
 * all. There is deliberately no "LaTeX" here — the format that used to carry
 * that name rendered exactly like markdown and compiled nothing, so people who
 * chose it expecting a PDF reported the TeX compiler as broken. "LaTeX" now
 * names only the project kind, which really does compile.
 */
export const DOCUMENT_KIND_LABELS: Record<DocumentKind, string> = {
  markdown: "Markdown",
  text: "Plain text",
};

/**
 * The long form of each view's name, for the topbar.
 *
 * There is no "sandbox" entry because there is no sandbox destination: running
 * code is something the agent does on your behalf, and what it produced shows
 * up on the tool card that produced it. The service and its API are still
 * there — nothing about this file turned them off.
 */
export const PAGE_TITLES: Record<View, string> = {
  chat: "Chat",
  // A group of threads with standing context — instructions, knowledge files,
  // its own memory shelf. Beside Chat because that is what it groups.
  spaces: "Spaces",
  agents: "Agents",
  skills: "Skills",
  sources: "Sources",
  memory: "Memory",
  graph: "Graph",
  dashboards: "Dashboards",
  // Programs the sandbox builds and publishes — releases, visibility, their
  // own frame. Split from Dashboards so neither word has to mean two things.
  apps: "Apps",
  // The immutable table versions dashboards bind to. They existed since ADR
  // 0003 with no page — created as an upload side effect, visible only inside
  // the app editor's binding chips.
  datasets: "Datasets",
  documents: "Documents",
  // One destination for boards and lists both: a list is a board with one
  // column, and splitting them into "Boards"/"Lists" pages made an object
  // teleport between them when its column count crossed 1.
  boards: "Boards & todos",
  data: "Databases",
  projects: "Projects",
  integrations: "Integrations",
  mcp: "MCP servers",
  // A tools-*management* destination, not the sandbox itself: you come here to
  // author the custom tools the agent may run and to set each one's egress and
  // approval, exactly as MCP is where you register servers. This is not the
  // "sandbox" destination the docstring above refuses — nobody operates a
  // machine here, they configure a capability.
  "sandbox-tools": "Sandbox tools",
  // The approval queue. "Activity" described the audit half of the page; the
  // half a user actually comes for is the requests waiting on them.
  activity: "Inbox",
  // The standing-grant ledger, with the org posture under it. Member-readable
  // on purpose: a rule you cannot see is indistinguishable from a bug.
  policies: "Rules & policies",
  admin: "Admin",
  workflows: "Workflows",
  // A cron is a schedule. Its old title, "Automations", is the *group* now.
  crons: "Schedules",
};

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function formatRelative(value: string): string {
  const delta = Date.now() - new Date(value).getTime();
  const minutes = Math.max(0, Math.floor(delta / 60_000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return new Date(value).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
}

/**
 * The rail's two audiences, split from the one list the server returns.
 *
 * `listConversations` returns the caller's OWN personal threads together with
 * every SHARED thread in the workspace and never another member's personal
 * thread, so `shared` alone tells the two groups apart — the rail must not try
 * to re-derive this from ownership and accidentally file a shared thread the
 * caller does not own under the wrong heading. Order within each group is left
 * untouched so the server's recency sort survives the split.
 */
export function groupThreads(conversations: Conversation[]): {
  personal: Conversation[];
  shared: Conversation[];
} {
  const personal: Conversation[] = [];
  const shared: Conversation[] = [];
  for (const conversation of conversations) {
    (conversation.shared ? shared : personal).push(conversation);
  }
  return { personal, shared };
}

/** The strings the rail's share/unshare toggle needs once it is allowed to render. */
export type ShareControl = {
  shared: boolean;
  title: string;
  ariaLabel: string;
  pressed: boolean;
};

/**
 * The share/unshare toggle for one rail row, or null when it must not render.
 *
 * The control rides ONLY the open thread and ONLY when the caller may change
 * its visibility (`can_share` — the thread's creator or a workspace owner). A
 * member seeing it on a thread they cannot act on would get a 403 on click,
 * which is worse than no control at all; returning null keeps that button out
 * of the tree rather than leaving a dead affordance on every row.
 */
export function shareControl(
  conversation: Conversation,
  activeConversationId: string | null,
): ShareControl | null {
  if (conversation.id !== activeConversationId || !conversation.can_share) {
    return null;
  }
  return {
    shared: conversation.shared,
    title: conversation.shared
      ? "Shared with the workspace — make personal"
      : "Share with the workspace",
    ariaLabel: conversation.shared
      ? `Make ${conversation.title} personal`
      : `Share ${conversation.title} with the workspace`,
    pressed: conversation.shared,
  };
}

/**
 * The name shown above a message.
 *
 * On a shared thread a user message carries the sender's name, so a teammate's
 * turn is not mislabelled "You"; on a personal thread every user message is the
 * caller's, so the name would be noise and the label stays "You". A message
 * with no attribution (one written before the sender column existed) also falls
 * back to "You". Anything that is not a user turn is the "Assistant".
 */
export function senderLabel(message: Message, sharedThread: boolean): string {
  if (message.role !== "user") return "Assistant";
  return sharedThread && message.sender_name ? message.sender_name : "You";
}

/**
 * The single-letter avatar for a message. A user's initial is taken from the
 * attributed sender on a shared thread — so two members show different marks —
 * and falls back to "U" on a personal thread or an unattributed message;
 * assistant and tool rows always show "A".
 */
export function senderInitial(message: Message, sharedThread: boolean): string {
  if (message.role !== "user") return "A";
  const source = sharedThread && message.sender_name ? message.sender_name : "U";
  return source.slice(0, 1).toUpperCase();
}

/**
 * Whether a message is the viewer's own to edit.
 *
 * On a personal thread every user message is the caller's — including rows
 * written before the sender column existed (`sender_id === ""`) — so all of
 * them are editable. On a shared thread only an exact attribution match will
 * do: an edit truncates everything after the message, and rewriting a
 * teammate's prompt would delete their turn out from under them (the server
 * 409s that case; this keeps the pencil off it in the first place). A legacy
 * ""-sender on a shared thread is unattributable, so nobody gets its pencil.
 */
export function senderIsViewer(
  message: Message,
  sharedThread: boolean,
  viewerId: string,
): boolean {
  if (message.role !== "user") return false;
  if (!sharedThread) return true;
  return viewerId !== "" && message.sender_id === viewerId;
}

export function statusLabel(status: Source["status"]): string {
  if (status === "ready") return "Indexed";
  if (status === "processing") return "Reading";
  if (status === "queued") return "Queued";
  if (status === "failed") return "Needs attention";
  // Held, never ingested: a chart a sandbox run drew. It was rendering as the
  // raw word "stored" because the union did not know the state existed.
  if (status === "stored") return "Saved";
  return status;
}
