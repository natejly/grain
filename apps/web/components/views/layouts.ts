import { sanitizePanes, type ChatPane } from "../chat-panes";
import { validSizes } from "./split-sizes";

/**
 * Saved layouts: a named snapshot of the whole chat split — which thread is
 * primary, which extra panes sit beside it, and the drag ratios between them —
 * recalled by name from the palette. Kept apart from the shell the way
 * `chat-panes` is kept apart from `useWorkspace`: the shell owns the state and
 * the storage reads, this owns the rules, and each function is total — given
 * any input it returns a valid store rather than throwing, because the
 * persisted value is one hand-editable key away from anything at all.
 *
 * Layouts are per workspace (`grain.layouts.<workspaceId>`): a split of one
 * workspace's threads means nothing in another. Switching workspaces remounts
 * the shell, so the key needs no invalidation — the fresh mount reads fresh.
 */

/** localStorage key prefix; the workspace id completes it. */
export const LAYOUTS_KEY_PREFIX = "grain.layouts.";

export type SavedLayout = {
  /** The thread in pane 0 when the layout was saved, or null for "whatever is
   *  open" — applying then keeps the current primary rather than switching. */
  primaryConversationId: string | null;
  /** The EXTRA panes beside the primary, under the same rules the live split
   *  persists by (string ids, unique, capped). */
  panes: ChatPane[];
  /** The drag ratios for `panes.length + 1` columns, or `[]` when none were
   *  saved — applying then leaves whatever ratios that column count last held. */
  sizes: number[];
};

/** The whole store: layouts by name. */
export type SavedLayouts = Record<string, SavedLayout>;

/**
 * Decode a persisted store. A missing, malformed, or hostile value is an empty
 * store, never a throw; each entry is re-validated whole — panes through the
 * same rules the live split trusts, sizes only at the length the panes imply —
 * and an entry that fails partially keeps what survives (bad sizes become
 * "none") rather than dropping a layout someone named on purpose.
 */
export function parseStoredLayouts(raw: string | null): SavedLayouts {
  if (!raw) return {};
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    const layouts: SavedLayouts = {};
    for (const [name, value] of Object.entries(parsed as Record<string, unknown>)) {
      // JSON.parse can hand back an OWN "__proto__" property; assigning it
      // below would rewrite this store's prototype instead of adding a key.
      if (!name.trim() || name === "__proto__") continue;
      if (!value || typeof value !== "object" || Array.isArray(value)) continue;
      const entry = value as Record<string, unknown>;
      const panes = sanitizePanes(entry.panes);
      layouts[name] = {
        primaryConversationId:
          typeof entry.primaryConversationId === "string"
            ? entry.primaryConversationId
            : null,
        panes,
        sizes: validSizes(entry.sizes, panes.length + 1) ? entry.sizes : [],
      };
    }
    return layouts;
  } catch {
    return {};
  }
}

/** Encode a store for persistence — the inverse of `parseStoredLayouts`. */
export function serializeStoredLayouts(layouts: SavedLayouts): string {
  return JSON.stringify(layouts);
}

/**
 * Add or replace the layout under `name` (trimmed — the palette's input can
 * carry edge whitespace). A blank name returns the store unchanged: nothing
 * unnamed can be recalled, so nothing unnamed is kept.
 */
export function upsertLayout(
  layouts: SavedLayouts,
  name: string,
  layout: SavedLayout,
): SavedLayouts {
  const trimmed = name.trim();
  if (!trimmed) return layouts;
  return { ...layouts, [trimmed]: layout };
}

/** Drop the layout under `name`, preserving the input's identity when there is
 *  nothing to drop so a React state setter can skip a pointless re-render. */
export function removeLayout(layouts: SavedLayouts, name: string): SavedLayouts {
  if (!(name in layouts)) return layouts;
  const next = { ...layouts };
  delete next[name];
  return next;
}

/** The saved names, alphabetical — a stable order for the palette's rows. */
export function layoutNames(layouts: SavedLayouts): string[] {
  return Object.keys(layouts).sort((a, b) => a.localeCompare(b));
}
