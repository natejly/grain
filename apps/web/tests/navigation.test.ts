import { describe, expect, it } from "vitest";
import {
  DEFAULT_GROUP_VIEW,
  NAV_GROUPS,
  groupForView,
} from "../components/views/navigation";
import { PAGE_TITLES, type View } from "../components/views/shared";

/**
 * The sidebar is generated from NAV_GROUPS, so a view that is missing from it
 * is a view with no way to reach it — and the shell would still render, which
 * is exactly the kind of silence this pins down. `groupForView` also falls back
 * to the first group rather than throwing, so an orphan would only show up as
 * Chat looking oddly highlighted.
 */
const ALL_VIEWS = Object.keys(PAGE_TITLES) as View[];

describe("navigation model", () => {
  it("gives every view exactly one group", () => {
    const placements = NAV_GROUPS.flatMap((group) => group.items.map((item) => item.view));
    expect([...placements].sort()).toEqual([...ALL_VIEWS].sort());
    expect(new Set(placements).size).toBe(placements.length);
  });

  it("keeps the sidebar to five groups", () => {
    // The whole point of the change: eleven top-level entries became five.
    expect(NAV_GROUPS.map((group) => group.label)).toEqual([
      "Chat",
      "Create",
      "Knowledge",
      "Connections",
      "Activity",
    ]);
  });

  it("resolves each view to the group that lists it", () => {
    for (const group of NAV_GROUPS) {
      for (const item of group.items) {
        expect(groupForView(item.view).id).toBe(group.id);
      }
    }
  });

  it("lands each group on its first item", () => {
    for (const group of NAV_GROUPS) {
      expect(DEFAULT_GROUP_VIEW[group.id]).toBe(group.items[0].view);
    }
  });

  it("gives memory a home under Knowledge", () => {
    expect(groupForView("memory").id).toBe("knowledge");
  });
});
