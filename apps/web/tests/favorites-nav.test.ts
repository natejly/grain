import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Favorite, FavoriteKind } from "@workspace/api-client";
import { MessageSquare } from "lucide-react";

/**
 * The Favorites block: one list the whole shell shares.
 *
 * Three claims earn tests here. The glyphs are *derived* from NAV_GROUPS, so a
 * favorite's icon and its destination's cannot diverge — the invariant, not
 * the pixels. Reorder posts the FULL re-ordinal'd list in one write, because
 * the server owns order as a block and a delta would leave the other rows'
 * ordinals to guesswork. And an empty list renders nothing at all — a heading
 * over no rows is furniture, and the e2e specs pin the sidebar's shape around
 * this block being absent until someone stars something.
 */

const listFavorites = vi.fn();
const addFavorite = vi.fn();
const removeFavorite = vi.fn();
const saveFavoritesOrder = vi.fn();

vi.mock("../components/api", () => ({
  api: {
    listFavorites: (...a: unknown[]) => listFavorites(...a),
    addFavorite: (...a: unknown[]) => addFavorite(...a),
    removeFavorite: (...a: unknown[]) => removeFavorite(...a),
    saveFavoritesOrder: (...a: unknown[]) => saveFavoritesOrder(...a),
  },
}));

import {
  FAVORITE_VIEW,
  FavoriteStar,
  FavoritesNav,
  favoriteIcon,
  useFavorites,
} from "../components/views/favorites";
import { groupForView } from "../components/views/navigation";

const FAVS: Favorite[] = [
  { kind: "document", target_id: "doc-1", label: "Launch Runbook", ordinal: 0 },
  { kind: "conversation", target_id: "conv-1", label: "Planning thread", ordinal: 1 },
  { kind: "dashboard", target_id: "dash-1", label: "Revenue", ordinal: 2 },
];

/** The hook and the block together, the way the workspace mounts them. */
function Harness({ onOpen = () => undefined }: { onOpen?: (kind: FavoriteKind, id: string) => void }) {
  const favorites = useFavorites();
  return createElement(FavoritesNav, { favorites, onOpen });
}

/** The hook and one star, the way a header or catalog row mounts it. */
function StarHarness() {
  const favorites = useFavorites();
  return createElement(FavoriteStar, {
    kind: "dashboard",
    targetId: "dash-9",
    label: "Ops",
    favorites,
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  listFavorites.mockResolvedValue(FAVS);
});
afterEach(cleanup);

async function mount(onOpen?: (kind: FavoriteKind, id: string) => void) {
  render(createElement(Harness, { onOpen }));
  await waitFor(() => expect(screen.getByText("Launch Runbook")).toBeTruthy());
}

describe("glyph derivation", () => {
  it("borrows every kind's icon from the nav item of the view it opens", () => {
    // One definition per thing: the sidebar row and the destination it lands
    // on must show the same glyph, and the only way to guarantee that is to
    // have no second declaration to drift.
    for (const [kind, view] of Object.entries(FAVORITE_VIEW)) {
      const item = groupForView(view).items.find((candidate) => candidate.view === view);
      expect(item, `${kind} names a view no group lists`).toBeTruthy();
      expect(favoriteIcon(kind as FavoriteKind)).toBe(item?.icon);
    }
  });

  it("gives threads the MessageSquare a thread row wears", () => {
    expect(favoriteIcon("conversation")).toBe(MessageSquare);
  });
});

describe("FavoritesNav", () => {
  it("renders the rows in order under their own labelled nav", async () => {
    await mount();
    const nav = screen.getByRole("navigation", { name: "Favorites" });
    const labels = Array.from(nav.querySelectorAll(".favorite-open span")).map(
      (node) => node.textContent,
    );
    expect(labels).toEqual(["Launch Runbook", "Planning thread", "Revenue"]);
  });

  it("opens a row through the shell's per-kind landing", async () => {
    const onOpen = vi.fn();
    await mount(onOpen);
    fireEvent.click(screen.getByText("Planning thread"));
    expect(onOpen).toHaveBeenCalledWith("conversation", "conv-1");
  });

  it("unfavorites through the client and drops the row", async () => {
    await mount();
    removeFavorite.mockResolvedValue(undefined);
    fireEvent.click(
      screen.getByRole("button", { name: "Remove Launch Runbook from favorites" }),
    );
    await waitFor(() => expect(removeFavorite).toHaveBeenCalledWith("document", "doc-1"));
    await waitFor(() => expect(screen.queryByText("Launch Runbook")).toBeNull());
  });

  it("reorders by posting the whole block's ordinals, not a delta", async () => {
    await mount();
    const swapped: Favorite[] = [
      { ...FAVS[1], ordinal: 0 },
      { ...FAVS[0], ordinal: 1 },
      { ...FAVS[2], ordinal: 2 },
    ];
    saveFavoritesOrder.mockResolvedValue(swapped);
    fireEvent.click(screen.getByRole("button", { name: "Move Launch Runbook down" }));
    await waitFor(() =>
      expect(saveFavoritesOrder).toHaveBeenCalledWith([
        { kind: "conversation", target_id: "conv-1", ordinal: 0 },
        { kind: "document", target_id: "doc-1", ordinal: 1 },
        { kind: "dashboard", target_id: "dash-1", ordinal: 2 },
      ]),
    );
    // The server's echo is the state: the rows land in the answered order.
    const nav = screen.getByRole("navigation", { name: "Favorites" });
    await waitFor(() =>
      expect(nav.querySelector(".favorite-open span")?.textContent).toBe(
        "Planning thread",
      ),
    );
    // A kept move is announced to assistive tech — a sighted user watches the
    // row travel; the live region tells everyone else.
    expect(
      screen.getByText("Launch Runbook moved to position 2 of 3"),
    ).toBeTruthy();
  });

  it("refuses a move past an edge, with the button still focusable", async () => {
    await mount();
    const up = screen.getByRole("button", { name: "Move Launch Runbook up" });
    // aria-disabled, not disabled: focus must survive reaching either end —
    // the board chevrons' contract.
    expect(up.getAttribute("aria-disabled")).toBe("true");
    expect((up as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(up);
    expect(saveFavoritesOrder).not.toHaveBeenCalled();
  });

  it("renders nothing at all while the list is empty", async () => {
    listFavorites.mockResolvedValue([]);
    render(createElement(Harness, {}));
    await waitFor(() => expect(listFavorites).toHaveBeenCalled());
    expect(screen.queryByRole("navigation", { name: "Favorites" })).toBeNull();
    expect(screen.queryByText("Favorites")).toBeNull();
  });
});

describe("FavoriteStar", () => {
  it("adds through the client and flips its name and pressed state", async () => {
    addFavorite.mockResolvedValue({
      kind: "dashboard",
      target_id: "dash-9",
      label: "Ops",
      ordinal: 3,
    });
    render(createElement(StarHarness));
    // Not yet starred: the name carries the outcome, the state says off.
    const star = await screen.findByRole("button", { name: "Favorite Ops" });
    expect(star.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(star);
    await waitFor(() => expect(addFavorite).toHaveBeenCalledWith("dashboard", "dash-9"));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Unfavorite Ops" }).getAttribute("aria-pressed")).toBe("true"),
    );
  });
});
