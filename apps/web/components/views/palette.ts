import type { Conversation } from "@workspace/api-client";
import { CREATE_ACTIONS, NAV_GROUPS, type CreateAction } from "./navigation";
import { PAGE_TITLES, type View } from "./shared";

/**
 * The command palette's data model: what ⌘K can reach, flattened into rows a
 * matcher can rank. Pure functions, no React — the shell renders the result
 * and the tests exercise the ranking without a DOM.
 *
 * Three kinds of row, in the order a query resolves them:
 * - "view": every destination in NAV_GROUPS, rail and settings alike. The
 *   palette is how a hidden-but-reachable surface stays reachable.
 * - "create": the same six actions the + Create menu offers, so nothing is
 *   creatable from one surface and not the other.
 * - "thread": the rail's conversations, searchable by title — the "find that
 *   chat from Tuesday" the rail's recency sort cannot answer.
 */
export type PaletteRow =
  | { kind: "view"; view: View; label: string; hint: string }
  | { kind: "create"; action: CreateAction; label: string; hint: string }
  | { kind: "thread"; conversationId: string; label: string; hint: string };

export function buildPaletteRows(conversations: Conversation[]): PaletteRow[] {
  const views: PaletteRow[] = NAV_GROUPS.flatMap((group) =>
    group.items.map((item) => ({
      kind: "view" as const,
      view: item.view,
      // The long form, so "MCP servers" is findable by either word; the hint
      // says which door it is behind.
      label: PAGE_TITLES[item.view],
      hint: group.surface === "settings" ? "Settings" : group.label,
    })),
  );
  const creates: PaletteRow[] = CREATE_ACTIONS.map((action) => ({
    kind: "create" as const,
    action,
    label: `New ${action.noun}`,
    hint: "Create",
  }));
  const threads: PaletteRow[] = conversations.map((conversation) => ({
    kind: "thread" as const,
    conversationId: conversation.id,
    label: conversation.title,
    hint: conversation.shared ? "Shared thread" : "Thread",
  }));
  return [...views, ...creates, ...threads];
}

/**
 * Rank rows against a query. Three tiers, stable within each: label starts
 * with the query, then a word in the label starts with it, then it appears
 * anywhere. Case-insensitive. An empty query returns the navigation — views
 * and creates — because "what can I even do" is the empty palette's question;
 * threads appear once there is something to match, since an unfiltered
 * thread list is what the rail already is.
 */
export function matchPalette(
  rows: PaletteRow[],
  query: string,
  limit = 12,
): PaletteRow[] {
  const needle = query.trim().toLowerCase();
  if (!needle) {
    return rows.filter((row) => row.kind !== "thread").slice(0, limit);
  }
  const tiers: PaletteRow[][] = [[], [], []];
  for (const row of rows) {
    const label = row.label.toLowerCase();
    if (label.startsWith(needle)) tiers[0].push(row);
    else if (label.split(/\s+/).some((word) => word.startsWith(needle))) tiers[1].push(row);
    else if (label.includes(needle)) tiers[2].push(row);
  }
  return tiers.flat().slice(0, limit);
}
