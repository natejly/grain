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
  | "data"
  | "projects"
  | "sandbox"
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
 * What each document format is called on screen.
 *
 * The stored value for the second one is still "latex" — the API, the agent's
 * tools and every existing row use it, and renaming the wire value would be a
 * migration for a word. The *label* had to change: a "latex" document is
 * markdown with KaTeX maths and produces no PDF, so users who picked it
 * expecting one reported the TeX compiler as broken. "LaTeX" now names only
 * the LaTeX project kind, which really does compile.
 */
export const DOCUMENT_KIND_LABELS: Record<DocumentKind, string> = {
  markdown: "Markdown",
  latex: "Markdown + math",
};

export const PAGE_TITLES: Record<View, string> = {
  chat: "Chat",
  sources: "Sources",
  memory: "Memory",
  graph: "Graph",
  dashboards: "Dashboards",
  documents: "Documents",
  boards: "Boards",
  data: "Databases",
  projects: "Projects",
  sandbox: "Sandbox",
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
