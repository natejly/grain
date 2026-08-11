import { describe, expect, it } from "vitest";
import {
  CREATE_ACTIONS,
  DEFAULT_GROUP_VIEW,
  NAV_GROUPS,
  RAIL_GROUPS,
  SETTINGS_GROUPS,
  groupForView,
} from "../components/views/navigation";
import { PAGE_TITLES, type View } from "../components/views/shared";

/**
 * Both surfaces — the rail and the Settings menu — and every tab strip are
 * generated from NAV_GROUPS, so a view missing from it is a view with no way to
 * reach it, and the shell would still render. That silence is what these pin
 * down. `groupForView` also falls back to the first group rather than throwing,
 * so an orphan would only show up as Chat looking oddly highlighted.
 */
const ALL_VIEWS = Object.keys(PAGE_TITLES) as View[];

describe("navigation model", () => {
  it("gives every view exactly one group", () => {
    const placements = NAV_GROUPS.flatMap((group) => group.items.map((item) => item.view));
    expect([...placements].sort()).toEqual([...ALL_VIEWS].sort());
    expect(new Set(placements).size).toBe(placements.length);
  });

  it("puts the places you work on the rail", () => {
    // Create left the rail because creating is an action, not a destination.
    // Documents took its place — and its siblings' tab strip with it.
    expect(RAIL_GROUPS.map((group) => group.label)).toEqual([
      "Chat",
      "Documents",
      "Knowledge",
      "Workflows",
    ]);
  });

  it("keeps Workflows on the rail rather than behind Settings", () => {
    // Half of what the surface does is operating, not configuring: a run that
    // parked on an approval at 3am is waiting for someone to *see* it, and
    // Settings is the menu you open rarely and on purpose.
    expect(groupForView("workflows").surface).toBe("rail");
    // And it was appended, so the three destinations that were already on the
    // rail did not move under a user who knew where they were.
    expect(RAIL_GROUPS[RAIL_GROUPS.length - 1].id).toBe("workflows");
  });

  it("puts the places you configure behind Settings", () => {
    expect(SETTINGS_GROUPS.map((group) => group.label)).toEqual([
      "Connections",
      "Activity",
      "Admin",
    ]);
  });

  it("shows every group on exactly one surface", () => {
    expect([...RAIL_GROUPS, ...SETTINGS_GROUPS].map((group) => group.id).sort())
      .toEqual(NAV_GROUPS.map((group) => group.id).sort());
    const rail = new Set(RAIL_GROUPS.map((group) => group.id));
    expect(SETTINGS_GROUPS.some((group) => rail.has(group.id))).toBe(false);
  });

  it("keeps Documents' siblings reachable from its tab strip", () => {
    // Moving Documents up must not strand the four views that shared its group.
    const documents = NAV_GROUPS.find((group) => group.id === "documents");
    expect(documents?.items.map((item) => item.view)).toEqual([
      "documents",
      "projects",
      "sandbox",
      "boards",
      "dashboards",
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

  it("gives memory a home under Knowledge, on the rail", () => {
    // The reason Knowledge did not follow Connections into Settings: a user who
    // could not find their memories is not helped by burying them deeper.
    expect(groupForView("memory").id).toBe("knowledge");
    expect(groupForView("memory").surface).toBe("rail");
  });
});

describe("create actions", () => {
  it("offers the seven things a user can make", () => {
    expect(CREATE_ACTIONS.map((action) => action.label)).toEqual([
      "Document",
      "Project",
      "LaTeX document",
      "Sandbox",
      "Board",
      "Dashboard",
      "Workflow",
    ]);
  });

  it("offers LaTeX only as the project kind that compiles to a PDF", () => {
    // The bug this closes: "LaTeX" named a *document* format that renders
    // KaTeX maths and emits no PDF, so the word had two meanings and the
    // shallower one won. Exactly one create action may say LaTeX, and it must
    // make a project — projects are where the TeX engine runs.
    const latex = CREATE_ACTIONS.filter((action) => /latex/i.test(action.label));
    expect(latex.map((action) => action.id)).toEqual(["latex"]);
    expect(latex[0].view).toBe("projects");
  });

  it("names each thing the way it reads mid-sentence", () => {
    // "New {noun}" and "Create {noun}" are built from this, and lowercasing the
    // label instead would print "latex document" — the wordmark is the whole
    // point of that entry.
    for (const action of CREATE_ACTIONS) {
      expect(action.noun.toLowerCase()).toBe(action.label.toLowerCase());
    }
    expect(CREATE_ACTIONS.find((action) => action.id === "latex")?.noun).toBe(
      "LaTeX document",
    );
  });

  it("targets only views the navigation can reach", () => {
    const placed = new Set(
      NAV_GROUPS.flatMap((group) => group.items.map((item) => item.view)),
    );
    for (const action of CREATE_ACTIONS) {
      expect(placed.has(action.view)).toBe(true);
    }
  });

  it("borrows each icon from the nav item it opens", () => {
    // One definition per thing, so the menu and the tab strip cannot disagree
    // about what a Board looks like.
    for (const action of CREATE_ACTIONS) {
      const item = groupForView(action.view).items.find(
        (candidate) => candidate.view === action.view,
      );
      expect(action.icon).toBe(item?.icon);
    }
  });

  it("asks for a name only where one cannot be supplied later", () => {
    const prompts = Object.fromEntries(
      CREATE_ACTIONS.map((action) => [action.id, action.prompt]),
    );
    expect(prompts.document).toBeTruthy();
    expect(prompts.project).toBeTruthy();
    expect(prompts.board).toBeTruthy();
    // A sandbox is a machine, and a dashboard is named inside its own editor.
    expect(prompts.sandbox).toBe("");
    expect(prompts.dashboard).toBe("");
    // A workflow is named by the compiler from the sentence it was asked for,
    // so a name typed beforehand would be thrown away.
    expect(prompts.workflow).toBe("");
  });
});
