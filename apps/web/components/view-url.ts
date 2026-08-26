import type { View } from "./views/shared";

/**
 * The runtime mirror of the `View` union in `shared.ts`. TypeScript can't
 * enumerate a string union at runtime, so a set is the cheapest way to fence a
 * `?view=` value that names something which is not a real view. Keep this in
 * sync with `View` when a view is added or retired.
 */
const VIEWS: ReadonlySet<string> = new Set<View>([
  "chat",
  "agents",
  "skills",
  "sources",
  "memory",
  "graph",
  "dashboards",
  "apps",
  "datasets",
  "integrations",
  "documents",
  "boards",
  "data",
  "projects",
  "mcp",
  "sandbox-tools",
  "sandbox-secrets",
  "webhooks",
  "activity",
  "policies",
  "admin",
  "workflows",
  "crons",
  "monitors",
  "spaces",
  "gallery",
]);

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

/**
 * Push the active view into the URL as `?view=…` so the view is deep-linkable
 * and back/forward moves between views. Uses `pushState` (a real history
 * entry), preserves any other query params already present, and no-ops when
 * the URL already holds this view so a repeated click doesn't stack entries.
 */
export function pushViewToUrl(view: View): void {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  if (url.searchParams.get("view") === view) return;
  url.searchParams.set("view", view);
  window.history.pushState({}, "", url);
}
