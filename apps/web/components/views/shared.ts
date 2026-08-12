import { ApiError, type DocumentKind, type Source } from "@workspace/api-client";

export type View =
  | "chat"
  | "sources"
  | "memory"
  | "graph"
  | "dashboards"
  | "integrations"
  | "documents"
  | "boards"
  | "todos"
  | "data"
  | "projects"
  | "mcp"
  | "activity"
  | "admin"
  | "workflows";

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
  sources: "Sources",
  memory: "Memory",
  graph: "Graph",
  dashboards: "Dashboards",
  documents: "Files",
  boards: "Boards",
  todos: "Lists",
  data: "Databases",
  projects: "Projects",
  integrations: "Integrations",
  mcp: "MCP servers",
  activity: "Activity",
  admin: "Admin",
  workflows: "Workflows",
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
