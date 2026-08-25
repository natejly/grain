"use client";

import type {
  Favorite,
  FavoriteKind,
  FavoriteOrderEntry,
} from "@workspace/api-client";
import { ChevronDown, ChevronUp, Star, X, type LucideIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { groupForView } from "./navigation";
import { describeError, type View } from "./shared";

/**
 * Favorites: the caller's own pin-anything block, above the sidebar's other
 * furniture in every destination.
 *
 * The server owns the hard parts — labels are resolved at listing time under
 * each kind's visibility rule, and a target the caller can no longer see is
 * simply absent — so the client's whole job is one list, fetched once and
 * threaded everywhere. The hook is instantiated ONCE in the workspace shell
 * and passed down: a star in the Documents header and the row in the sidebar
 * are the same state, and two copies of the list would drift the moment one
 * of them toggled.
 */

/**
 * Where each kind of favorite lives. The glyph is *derived* from this view's
 * NAV_GROUPS entry rather than declared per kind, so a favorite's icon and
 * its destination's cannot diverge — the same one-definition rule the Create
 * menu follows. A conversation's view is Chat, whose icon is the
 * MessageSquare a thread row wants anyway.
 */
export const FAVORITE_VIEW: Record<FavoriteKind, View> = {
  conversation: "chat",
  agent: "agents",
  document: "documents",
  project: "projects",
  board: "boards",
  dashboard: "dashboards",
  workflow: "workflows",
  cron: "crons",
};

export function favoriteIcon(kind: FavoriteKind): LucideIcon {
  const view = FAVORITE_VIEW[kind];
  const item = groupForView(view).items.find((candidate) => candidate.view === view);
  // Unreachable while FAVORITE_VIEW names only views NAV_GROUPS lists —
  // navigation.test.ts pins that every view has a group — but a missing item
  // must not blank the row, and the Star at least says what the row is.
  return item?.icon ?? Star;
}

/** One string key per favorite, since (kind, target_id) is the identity. */
export function favoriteId(kind: FavoriteKind, targetId: string): string {
  return `${kind}:${targetId}`;
}

export type FavoritesApi = {
  /** The caller's favorites, labels resolved, in their chosen order. */
  favorites: Favorite[];
  /** `favoriteId(...)` membership, for the stars that ask "am I on?". */
  ids: Set<string>;
  /** Add or remove, decided by current membership. */
  toggle: (kind: FavoriteKind, targetId: string) => Promise<void>;
  /**
   * The whole block's order in one write, like the dashboard grid. Resolves
   * true only when the server kept it, so the reorder announcement cannot
   * claim a move the toast just apologised for.
   */
  reorder: (entries: FavoriteOrderEntry[]) => Promise<boolean>;
  refresh: () => Promise<void>;
};

export function useFavorites(onError?: (message: string) => void): FavoritesApi {
  const [favorites, setFavorites] = useState<Favorite[]>([]);
  // The list as of the latest render, readable from inside a queued write —
  // membership must be decided when a toggle RUNS, not when it was clicked.
  const favoritesRef = useRef<Favorite[]>(favorites);
  favoritesRef.current = favorites;
  /**
   * One write at a time. A toggle resolving mid-reorder (or the reverse) would
   * interleave two optimistic setFavorites over one server order, so every
   * write joins this chain: a call arriving while one is in flight awaits it,
   * then proceeds against the list that write left behind.
   */
  const chain = useRef<Promise<unknown>>(Promise.resolve());
  function enqueue<T>(work: () => Promise<T>): Promise<T> {
    const next = chain.current.then(work, work);
    // The chain itself never rejects, or one failed write would poison every
    // later one; the caller still gets `next`'s own settlement.
    chain.current = next.catch(() => undefined);
    return next;
  }

  const refresh = async () => {
    setFavorites(await api.listFavorites());
  };

  useEffect(() => {
    // Quiet on load: an unreachable API already has the red banner, and the
    // block's honest fallback is simply not rendering.
    void refresh().catch(() => undefined);
  }, []);

  const ids = new Set(favorites.map((row) => favoriteId(row.kind, row.target_id)));

  const toggle = (kind: FavoriteKind, targetId: string) =>
    enqueue(async () => {
      const present = favoritesRef.current.some(
        (row) => row.kind === kind && row.target_id === targetId,
      );
      try {
        if (present) {
          await api.removeFavorite(kind, targetId);
          setFavorites((current) =>
            current.filter((row) => !(row.kind === kind && row.target_id === targetId)),
          );
        } else {
          // The server answers with the resolved row — label included — so the
          // sidebar can show it without a second round trip. A target the
          // caller cannot see 404s here rather than filing a dead row.
          const added = await api.addFavorite(kind, targetId);
          setFavorites((current) => [
            ...current.filter((row) => !(row.kind === kind && row.target_id === targetId)),
            added,
          ]);
        }
      } catch (caught) {
        onError?.(
          describeError(
            caught,
            present ? "Could not remove the favorite" : "Could not add the favorite",
          ),
        );
      }
    });

  const reorder = (entries: FavoriteOrderEntry[]) =>
    enqueue(async () => {
      try {
        // The server echoes the re-resolved list back, and that answer is the
        // state — a row whose target vanished mid-drag drops out here too.
        setFavorites(await api.saveFavoritesOrder(entries));
        return true;
      } catch (caught) {
        onError?.(describeError(caught, "Could not reorder favorites"));
        return false;
      }
    });

  return { favorites, ids, toggle, reorder, refresh };
}

export type FavoriteStarProps = {
  kind: FavoriteKind;
  targetId: string;
  /** What the target is called, for the accessible name. */
  label: string;
  favorites: FavoritesApi;
  className?: string;
  size?: number;
};

/**
 * The star that rides a thing where the thing lives — a thread row, the
 * Documents header, a catalog row. One component so every site says the same
 * sentence: the name carries the outcome ("Favorite …"/"Unfavorite …", the
 * catalog Pin button's pattern) and `aria-pressed` carries the state.
 */
export function FavoriteStar({
  kind,
  targetId,
  label,
  favorites,
  className = "icon-button",
  size = 14,
}: FavoriteStarProps) {
  const faved = favorites.ids.has(favoriteId(kind, targetId));
  const name = `${faved ? "Unfavorite" : "Favorite"} ${label}`;
  return (
    <button
      type="button"
      className={faved ? `${className} faved` : className}
      title={name}
      aria-label={name}
      aria-pressed={faved}
      onClick={(event) => {
        // Rows put the star inside a clickable surface; starring must not
        // also open the thing.
        event.stopPropagation();
        void favorites.toggle(kind, targetId);
      }}
    >
      <Star size={size} fill={faved ? "currentColor" : "none"} />
    </button>
  );
}

export type FavoritesNavProps = {
  favorites: FavoritesApi;
  /** The shell's per-kind landing: it owns the view state this needs. */
  onOpen: (kind: FavoriteKind, targetId: string) => void;
};

/**
 * The sidebar block itself. Renders nothing while the list is empty — a
 * heading over no rows would be furniture — and sits ABOVE the pinned
 * dashboards, which keep their own nav untouched.
 */
export function FavoritesNav({ favorites, onOpen }: FavoritesNavProps) {
  // A second chevron press before the reorder lands would be computed from
  // the stale index and silently lost — the board chevrons' guard, and
  // aria-disabled rather than disabled for their reason too: focus must
  // survive reaching either end.
  const [moving, setMoving] = useState(false);
  // What the last successful move did, for the live region below — a sighted
  // user watches the row travel; a screen reader is told.
  const [announcement, setAnnouncement] = useState("");
  const rows = favorites.favorites;

  async function step(index: number, delta: number) {
    const target = index + delta;
    if (moving || target < 0 || target >= rows.length) return;
    const next = [...rows];
    const [row] = next.splice(index, 1);
    next.splice(target, 0, row);
    setMoving(true);
    try {
      // The full re-ordinal'd list, not a delta: order is one write.
      const kept = await favorites.reorder(
        next.map((entry, ordinal) => ({
          kind: entry.kind,
          target_id: entry.target_id,
          ordinal,
        })),
      );
      // Only a move the server kept is announced — the refusal already toasts.
      if (kept) {
        setAnnouncement(`${row.label} moved to position ${target + 1} of ${next.length}`);
      }
    } finally {
      setMoving(false);
    }
  }

  if (rows.length === 0) return null;

  return (
    <nav className="favorites-nav" aria-label="Favorites">
      <span className="favorites-heading">Favorites</span>
      {/* Always in the tree so the live region exists before its first
          update, which is what makes assistive tech read the update. */}
      <span className="visually-hidden" aria-live="polite">
        {announcement}
      </span>
      {rows.map((row, index) => {
        const Icon = favoriteIcon(row.kind);
        return (
          <div className="favorite-row" key={favoriteId(row.kind, row.target_id)}>
            <button
              type="button"
              className="favorite-open"
              onClick={() => onOpen(row.kind, row.target_id)}
            >
              <Icon size={15} aria-hidden="true" />
              <span>{row.label}</span>
            </button>
            <button
              type="button"
              className="icon-button favorite-action"
              aria-disabled={index === 0 || moving}
              aria-label={`Move ${row.label} up`}
              onClick={() => void step(index, -1)}
            >
              <ChevronUp size={12} />
            </button>
            <button
              type="button"
              className="icon-button favorite-action"
              aria-disabled={index === rows.length - 1 || moving}
              aria-label={`Move ${row.label} down`}
              onClick={() => void step(index, 1)}
            >
              <ChevronDown size={12} />
            </button>
            <button
              type="button"
              className="icon-button favorite-action"
              aria-label={`Remove ${row.label} from favorites`}
              onClick={() => void favorites.toggle(row.kind, row.target_id)}
            >
              <X size={12} />
            </button>
          </div>
        );
      })}
    </nav>
  );
}
