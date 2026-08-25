import type { Conversation } from "@workspace/api-client";
import { chordHint } from "./chords";
import { CREATE_ACTIONS, NAV_GROUPS, type CreateAction } from "./navigation";
import { PAGE_TITLES, type View } from "./shared";
import type { ThreadOpen } from "./thread-open";

/**
 * The command palette's data model: what ⌘K can reach, flattened into rows a
 * matcher can rank. Pure functions, no React — the shell renders the result
 * and the tests exercise the ranking without a DOM.
 *
 * The kinds of row, in the order a query resolves them:
 * - "view": every destination in NAV_GROUPS, rail and settings alike. The
 *   palette is how a hidden-but-reachable surface stays reachable.
 * - "create": the same six actions the + Create menu offers, so nothing is
 *   creatable from one surface and not the other.
 * - "layout" / "save-layout": the saved split layouts by name, and the row
 *   that names a new one — recall and capture live side by side.
 * - "toggle": the two preferences the palette owns (where threads open, the
 *   G-chord kill-switch), labelled by their CURRENT state.
 * - "thread": the rail's conversations, searchable by title — the "find that
 *   chat from Tuesday" the rail's recency sort cannot answer.
 */
export type PaletteToggle = "thread-open" | "chords";

export type PaletteRow =
  | {
      kind: "view";
      view: View;
      label: string;
      hint: string;
      /** The "G C"-style chord, on the rows that have one — each row teaches
       *  the faster path to itself. */
      shortcut?: string;
    }
  | { kind: "create"; action: CreateAction; label: string; hint: string }
  | { kind: "layout"; name: string; label: string; hint: string }
  | { kind: "save-layout"; label: string; hint: string }
  | { kind: "toggle"; toggle: PaletteToggle; label: string; hint: string }
  | { kind: "thread"; conversationId: string; label: string; hint: string };

/**
 * The shell state the layout and preference rows are built from. Optional on
 * `buildPaletteRows` so the palette still stands alone in tests and simpler
 * hosts; without it only the original three kinds appear.
 */
export type PaletteExtras = {
  layoutNames: string[];
  threadOpen: ThreadOpen;
  chordsEnabled: boolean;
};

export function buildPaletteRows(
  conversations: Conversation[],
  extras?: PaletteExtras,
): PaletteRow[] {
  const views: PaletteRow[] = NAV_GROUPS.flatMap((group) =>
    group.items.map((item) => ({
      kind: "view" as const,
      view: item.view,
      // The long form, so "MCP servers" is findable by either word; the hint
      // says which door it is behind.
      label: PAGE_TITLES[item.view],
      hint: group.surface === "settings" ? "Settings" : group.label,
      // A chord hint on a disabled chord would teach a key that does nothing.
      shortcut:
        extras && !extras.chordsEnabled ? undefined : (chordHint(item.view) ?? undefined),
    })),
  );
  const creates: PaletteRow[] = CREATE_ACTIONS.map((action) => ({
    kind: "create" as const,
    action,
    label: `New ${action.noun}`,
    hint: "Create",
  }));
  const layouts: PaletteRow[] = extras
    ? [
        ...extras.layoutNames.map((name) => ({
          kind: "layout" as const,
          name,
          label: `Layout: ${name}`,
          // The delete gestures are appended by the RENDERER, which knows
          // whether a deleteLayout handler is actually wired — a hint baked
          // here would advertise a gesture some hosts cannot honor.
          hint: "Layout",
        })),
        { kind: "save-layout" as const, label: "Save layout as…", hint: "Layout" },
      ]
    : [];
  // Each toggle row SAYS the current state; Enter flips it. The label changes
  // with the state, so the row always reads as a fact, never a stale promise.
  const toggles: PaletteRow[] = extras
    ? [
        {
          kind: "toggle" as const,
          toggle: "thread-open" as const,
          label:
            extras.threadOpen === "split"
              ? "Threads open: in a split"
              : "Threads open: in place",
          hint: "Preference",
        },
        {
          kind: "toggle" as const,
          toggle: "chords" as const,
          label: extras.chordsEnabled
            ? "Keyboard shortcuts: on"
            : "Keyboard shortcuts: off",
          hint: "Preference",
        },
      ]
    : [];
  const threads: PaletteRow[] = conversations.map((conversation) => ({
    kind: "thread" as const,
    conversationId: conversation.id,
    label: conversation.title,
    hint: conversation.shared ? "Shared thread" : "Thread",
  }));
  return [...views, ...creates, ...layouts, ...toggles, ...threads];
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
    // Everything navigable and doable, UNSLICED: the empty palette's question
    // is "what can I even do", and a cap of 12 was silently eating every row
    // after the ~22 views — creates, layouts and the preference toggles were
    // undiscoverable from the very surface that exists to surface them. The
    // list scrolls; a hidden capability does not.
    return rows.filter((row) => row.kind !== "thread");
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
