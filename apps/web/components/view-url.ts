import type { View } from "./views/shared";

/**
 * The runtime mirror of the `View` union in `shared.ts`. TypeScript can't
 * enumerate a string union at runtime, so a record keyed by `View` is the
 * fence: a view added to the union without a row here fails typecheck, and a
 * retired one becomes an excess key. "Keep this in sync by hand" already
 * failed once — `recap` and `profile` reached the union but not the old set,
 * so reloading either page silently fell back to chat.
 */
const VIEW_ROWS = {
  chat: true,
  agents: true,
  skills: true,
  sources: true,
  memory: true,
  graph: true,
  dashboards: true,
  apps: true,
  datasets: true,
  integrations: true,
  documents: true,
  boards: true,
  data: true,
  projects: true,
  mcp: true,
  "sandbox-tools": true,
  "sandbox-secrets": true,
  webhooks: true,
  activity: true,
  policies: true,
  admin: true,
  workflows: true,
  crons: true,
  monitors: true,
  spaces: true,
  gallery: true,
  recap: true,
  profile: true,
  pages: true,
  watches: true,
  deliverables: true,
} satisfies Record<View, true>;

const VIEWS: ReadonlySet<string> = new Set(Object.keys(VIEW_ROWS));

export function isView(value: string | null | undefined): value is View {
  return Boolean(value && VIEWS.has(value));
}

/**
 * Read the active view from a URL search string (`?view=…`). Returns null
 * when the param is absent or names something that is not a real view, so the
 * caller can fall back to its default rather than render an unknown screen.
 */
export function viewFromUrl(search: string): View | null {
  const candidate = new URLSearchParams(search).get("view");
  return isView(candidate) ? candidate : null;
}

/** The focused-thread param riding beside `?view=` — `?t=<conversationId>`. */
const THREAD_PARAM = "t";

/**
 * Read the focused thread from a URL search string (`?t=…`). Returns the raw
 * value or null — deliberately no validation here, because a thread id is only
 * checkable against the loaded conversations, which the consumer holds. The
 * consumer fences unknown ids exactly the way `viewFromUrl`'s isView fence
 * does for views: silently, falling back to the default.
 */
export function threadFromUrl(search: string): string | null {
  return new URLSearchParams(search).get(THREAD_PARAM);
}

/**
 * The one chokepoint for the whole workspace URL: `?view=…` plus, on the chat
 * view with a thread focused, `?t=<threadId>`. `t` is set only when the view
 * is chat AND a thread id is given, and deleted otherwise — a stale thread
 * param under another view would deep-link to a screen that never reads it.
 * No-ops (zero history entries) when BOTH params already match, so coalesced
 * or repeated writes never stack entries and back/forward never hops ghosts.
 */
/**
 * Drop both workspace params from the address bar in place, for a workspace
 * switch. The shell remounts on the new workspace and re-parks whatever
 * `?t=`/`?view=` say — the known-thread fence silently drops a foreign
 * thread id, but nothing used to rewrite the URL, so the bar kept claiming
 * workspace A's thread over workspace B's screen and a copied link degraded
 * silently for whoever opened it. `replaceState`, not push: leaving a
 * workspace is not a navigation inside one, and the entry being corrected is
 * the one already on top.
 */
export function clearWorkspaceUrl(): void {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  if (!url.searchParams.has("view") && !url.searchParams.has(THREAD_PARAM)) return;
  url.searchParams.delete("view");
  url.searchParams.delete(THREAD_PARAM);
  window.history.replaceState({}, "", url);
}

export function pushWorkspaceUrl(view: View, threadId: string | null): void {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  const thread = view === "chat" && threadId !== null ? threadId : null;
  if (
    url.searchParams.get("view") === view &&
    url.searchParams.get(THREAD_PARAM) === thread
  ) {
    return;
  }
  url.searchParams.set("view", view);
  if (thread === null) url.searchParams.delete(THREAD_PARAM);
  else url.searchParams.set(THREAD_PARAM, thread);
  window.history.pushState({}, "", url);
}
