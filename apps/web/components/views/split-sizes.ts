/**
 * Split-sizes: the pure resize rules behind the multi-pane chat split, kept
 * apart from `ChatSplit` the way `chat-panes` is kept apart from
 * `useWorkspace` — the component owns the pointer events and the state, this
 * owns the arithmetic and the round trip through localStorage. Each function
 * is total: given any input it returns a valid layout rather than throwing,
 * because the persisted value is one hand-editable key away from anything.
 *
 * Sizes are flex-grow ratios summing to 100, and they are stored PER COLUMN
 * COUNT (`{"2":[60,40],"3":[34,33,33]}`): opening a third pane re-shares the
 * width evenly, but closing back to two restores the drag the user made at
 * two — a count change stops being a reset that wipes their work.
 */

/** localStorage key for the drag ratios, following the `grain.*` convention. */
export const SPLIT_SIZES_KEY = "grain.split-sizes";

/** Equal shares for `count` columns, as flex-grow ratios summing to 100. */
export function equalSizes(count: number): number[] {
  return Array.from({ length: count }, () => 100 / count);
}

/**
 * Move the divider at `index` by `deltaPercent`, returning the SAME array
 * unchanged when the move would push either neighbour below `min` — neither
 * pane may collapse behind its own divider, and identity is what lets a React
 * state setter skip a pointless re-render on a refused move.
 */
export function applyDelta(
  sizes: number[],
  index: number,
  deltaPercent: number,
  min: number,
): number[] {
  const left = sizes[index] + deltaPercent;
  const right = sizes[index + 1] - deltaPercent;
  if (!(left >= min) || !(right >= min)) return sizes;
  const next = [...sizes];
  next[index] = left;
  next[index + 1] = right;
  return next;
}

/** A stored ratio list is only trusted whole: right length, every entry a
 *  finite number at least visible-ish, and the sum near enough 100 that the
 *  flex-grow shares mean what they say. Exported for the saved-layouts store,
 *  which holds a ratio list of its own to the same standard. */
export function validSizes(value: unknown, count: number): value is number[] {
  if (!Array.isArray(value) || value.length !== count) return false;
  if (!value.every((entry) => typeof entry === "number" && Number.isFinite(entry) && entry > 0)) {
    return false;
  }
  const sum = value.reduce((total: number, entry: number) => total + entry, 0);
  return Math.abs(sum - 100) <= 1;
}

/**
 * Decode the stored ratios for `count` columns. A missing, malformed, or
 * hostile value — wrong length, non-numeric entries, a sum far from 100 —
 * is an even split, never a throw.
 */
export function parseStoredSizes(raw: string | null, count: number): number[] {
  if (!raw) return equalSizes(count);
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return equalSizes(count);
    }
    const entry = (parsed as Record<string, unknown>)[String(count)];
    return validSizes(entry, count) ? entry : equalSizes(count);
  } catch {
    return equalSizes(count);
  }
}

/**
 * Encode `sizes` for `count` columns on top of the existing store, keeping
 * every other count's entry — the inverse of `parseStoredSizes`, per key.
 * A prior value that does not parse is simply started over.
 */
export function serializeStoredSizes(
  previousRaw: string | null,
  count: number,
  sizes: number[],
): string {
  let kept: Record<string, unknown> = {};
  if (previousRaw) {
    try {
      const parsed: unknown = JSON.parse(previousRaw);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        kept = { ...(parsed as Record<string, unknown>) };
      }
    } catch {
      // Malformed store: overwrite rather than crash the drag that ends here.
    }
  }
  kept[String(count)] = sizes;
  return JSON.stringify(kept);
}
