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
 * Both surfaces — the rail and the workspace-settings menu — and every tab
 * strip are generated from NAV_GROUPS, so a view missing from it is a view with
 * no way to reach it, and the shell would still render. That silence is what
 * these pin down. `groupForView` also falls back to the first group rather than
 * throwing, so an orphan would only show up as Chat looking oddly highlighted.
 */
const ALL_VIEWS = Object.keys(PAGE_TITLES) as View[];

describe("navigation model", () => {
  it("gives every view exactly one group", () => {
    const placements = NAV_GROUPS.flatMap((group) => group.items.map((item) => item.view));
    expect([...placements].sort()).toEqual([...ALL_VIEWS].sort());
    expect(new Set(placements).size).toBe(placements.length);
  });

  it("puts the four doors on the rail, and only those", () => {
    // The foyer: talk (Chat), what needs me (Inbox), where's my stuff
    // (Library), what runs without me (Automations). Knowledge is a Library
    // section rather than a fifth door — the rail seat existed to keep it out
    // of a Settings menu, and that menu is gone.
    expect(RAIL_GROUPS.map((group) => group.label)).toEqual([
      "Chat",
      "Inbox",
      "Library",
      "Automations",
    ]);
  });

  it("keeps every section item inside its group's flat list, in order", () => {
    // `items` is derived from `sections`; a hand-edited drift between the two
    // would make the sidebar and everything keyed off `items` disagree.
    for (const group of NAV_GROUPS) {
      expect(group.items).toEqual(group.sections.flatMap((section) => section.items));
    }
  });

  it("has no sandbox destination, because a sandbox is not a place", () => {
    // The product decision this encodes: capabilities are not destinations. You
    // ask for a chart and get one on the tool card that drew it; you do not go
    // and operate the machine, any more than you visit "the database".
    const placements: string[] = NAV_GROUPS.flatMap((group) =>
      group.items.map((item) => item.view as string),
    );
    expect(placements).not.toContain("sandbox");
    expect(Object.keys(PAGE_TITLES)).not.toContain("sandbox");
    const actions: string[] = CREATE_ACTIONS.map((action) => action.id as string);
    expect(actions).not.toContain("sandbox");
    expect(actions).not.toContain("folder");
  });

  it("puts the approval queue on the rail, with nothing waiting behind settings", () => {
    // The bug this closes: the queue lived behind a menu labelled "Settings",
    // where the count of runs parked on a human read as configuration noise.
    expect(groupForView("activity").id).toBe("inbox");
    expect(groupForView("activity").surface).toBe("rail");
    // The Rules ledger sits beside the queue that writes into it: the
    // "always allow" checkbox on an approval files a standing grant, and the
    // page it is taken back on must not be a different door. The queue stays
    // first — it is what the badge counts, and the group's landing view.
    const inbox = NAV_GROUPS.find((group) => group.id === "inbox");
    expect(inbox?.items.map((item) => [item.view, item.label])).toEqual([
      ["activity", "Inbox"],
      ["policies", "Rules"],
    ]);
    // And the settings surface holds only what really is configuration — no
    // group behind it may contain a surface that waits on a person.
    expect(SETTINGS_GROUPS.map((group) => group.label)).toEqual(["Connections", "Admin"]);
  });

  it("keeps Automations on the rail rather than behind settings", () => {
    // Half of what the surface does is operating, not configuring: a run that
    // parked on an approval at 3am is waiting for someone to *see* it.
    expect(groupForView("workflows").surface).toBe("rail");
    // Schedules is the cron tab's honest name — a cron is a schedule, and its
    // old label ("Automations") is what the group is called now.
    const automations = NAV_GROUPS.find((group) => group.id === "workflows");
    expect(automations?.label).toBe("Automations");
    expect(automations?.items.map((item) => item.label)).toEqual([
      "Workflows",
      "Schedules",
    ]);
  });

  it("shows every group on exactly one surface", () => {
    expect([...RAIL_GROUPS, ...SETTINGS_GROUPS].map((group) => group.id).sort())
      .toEqual(NAV_GROUPS.map((group) => group.id).sort());
    const rail = new Set(RAIL_GROUPS.map((group) => group.id));
    expect(SETTINGS_GROUPS.some((group) => rail.has(group.id))).toBe(false);
  });

  it("shelves the Library under honest headings, with no doubled breadcrumb", () => {
    // "Files / Files" is what the topbar used to read: the group and its first
    // tab shared a name. The group is Library; the sections are the shelves a
    // flat seven-tab strip could not be.
    const library = NAV_GROUPS.find((group) => group.id === "files");
    expect(library?.label).toBe("Library");
    expect(
      library?.sections.map((section) => [
        section.label,
        section.items.map((item) => item.view),
      ]),
    ).toEqual([
      ["", ["documents", "projects"]],
      // One destination for both: a list is a board with one column, and that
      // is an implementation detail nobody should have to know to find their
      // checklist. Two entries here used to make a list teleport the moment it
      // grew a second column; now the object stays put and changes its glyph.
      ["Boards & todos", ["boards"]],
      // Data beside the things drawn from it — Databases moved here from the
      // Settings menu, ending the connect-data → chart-it trek.
      ["Data", ["datasets", "data"]],
      ["Dashboards", ["dashboards", "apps"]],
      // Inside Library, one always-visible click from anywhere in it — nearer
      // than the old rail seat, which existed only to outrun a Settings menu.
      ["Knowledge", ["sources", "memory", "graph"]],
      // The marketplace: a shelf you take things from, so it lives with the
      // shelves. Publishing happens on the Skills page, where the things live.
      ["Gallery", ["gallery"]],
    ]);
    expect(library?.items[0].label).toBe("Documents");
    expect(PAGE_TITLES.documents).toBe("Documents");
    // No Library entry repeats the group's name: the topbar prints
    // "{group} / {PAGE_TITLES[view]}", which is where "Files / Files" came
    // from. (Chat is exempt by construction — its breadcrumb shows the open
    // thread's title, never the entry label.)
    for (const item of library?.items ?? []) {
      expect(PAGE_TITLES[item.view]).not.toBe(library?.label);
    }
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

  it("keeps memory on a working surface, never behind settings", () => {
    // Knowledge folded into Library, but the old rule holds its ground: a user
    // who could not find their memories is not helped by burying them behind a
    // gear. The Library sidebar shows the Knowledge heading the whole time you
    // are there, which is *more* visible than the old rail seat, not less.
    expect(groupForView("memory").id).toBe("files");
    expect(groupForView("memory").surface).toBe("rail");
  });
});

describe("create actions", () => {
  it("offers the six things a user can make", () => {
    // "App", not "Dashboard": the entry that said Dashboard opened the app
    // editor and built a sandbox program. Dashboards are written by the agent
    // during a conversation, so the menu does not offer to make one.
    expect(CREATE_ACTIONS.map((action) => action.label)).toEqual([
      "Document",
      "Project",
      "LaTeX document",
      "Board",
      "App",
      "Workflow",
    ]);
    expect(CREATE_ACTIONS.map((action) => action.id as string)).not.toContain(
      "dashboard",
    );
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
    // An app is named inside its own editor.
    expect(prompts.app).toBe("");
    // A workflow is named by the compiler from the sentence it was asked for,
    // so a name typed beforehand would be thrown away.
    expect(prompts.workflow).toBe("");
  });
});
