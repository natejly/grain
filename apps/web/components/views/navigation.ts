import {
  BarChart3,
  Blocks,
  Bot,
  Braces,
  Brain,
  Clock,
  Database,
  FileText,
  Inbox,
  KanbanSquare,
  Layers,
  LayoutGrid,
  Library,
  LibraryBig,
  MessageSquare,
  Network,
  Plug,
  ShieldCheck,
  Sparkles,
  Table2,
  Terminal,
  Workflow,
  type LucideIcon,
} from "lucide-react";
import type { View } from "./shared";

/**
 * One table describes the whole of navigation, because a view that is missing
 * from it is a view with no way to reach it — and the shell would still render.
 *
 * Two levels now, not one. The rail is four icons — Chat, Inbox, Library,
 * Automations — and each opens a contextual sidebar whose SECTIONS group that
 * destination's views under honest headings. The flat tab strip this replaces
 * had grown to seven tabs under Library alone, at which point a strip stops
 * being "the siblings at a glance" and becomes a second navigation problem.
 *
 * Knowledge lives inside Library rather than on the rail. It earned a rail
 * seat when the alternative was burial in a Settings menu; with Library's
 * sidebar always showing its section headings, Sources/Memory/Graph are one
 * click from anywhere and *visible* the whole time you are in Library — nearer
 * than the old rail seat left them, not further. Databases moved beside
 * Datasets for the same reason: connect-data and chart-it were a Settings
 * menu and a rail destination apart, and the audit called that trek out by
 * name.
 *
 * Inbox is on the rail because approvals are the product's primary work
 * object, not an audit artifact. It carries the shell's only numeric badge.
 *
 * Sandbox is not a destination anywhere, and that is the sharper decision: a
 * sandbox is a *capability*. You do not visit it any more than you visit the
 * database — you ask for a chart and the figure appears on the tool card in
 * the conversation that asked.
 */
export type GroupId =
  | "chat"
  | "inbox"
  | "files"
  | "workflows"
  | "connections"
  | "admin";

/**
 * Where a group is reached from. "rail" is the icon rail — the places you
 * work. "settings" is the workspace-settings menu — the places you configure,
 * which are visited rarely and do not deserve permanent rail space. Nothing
 * that *waits on a person* may live behind "settings".
 *
 * Both surfaces read this one list, so a group cannot appear on neither.
 */
export type NavSurface = "rail" | "settings";

export type NavItem = {
  view: View;
  /** Short label for the sidebar; PAGE_TITLES carries the long form. */
  label: string;
  icon: LucideIcon;
};

/**
 * One headed block of a destination's sidebar. `label: ""` renders no heading
 * — the unlabelled block a destination's primary views sit in.
 */
export type NavSection = {
  /**
   * Stable name for this shelf, unique within its group. The sidebar's
   * collapse state persists under `grain.section.<groupId>.<sectionId>`, so
   * this must not change when a label is reworded — "main" for the unlabelled
   * block, which never collapses.
   */
  id: string;
  label: string;
  items: NavItem[];
};

export type NavGroup = {
  id: GroupId;
  label: string;
  icon: LucideIcon;
  surface: NavSurface;
  sections: NavSection[];
  /** Every view in the group, in sidebar order. Derived from `sections`. */
  items: NavItem[];
};

/** The declaration shape: `items` is derived below, never written by hand. */
type NavGroupSpec = Omit<NavGroup, "items">;

const GROUP_SPECS: NavGroupSpec[] = [
  {
    id: "chat",
    label: "Chat",
    icon: MessageSquare,
    surface: "rail",
    /**
     * Agents live beside Chat, not in settings: an agent is who you are
     * talking to, so the place you author one is a click from the place you
     * use one. A skill is the same argument in the other dimension — not who
     * answers but what you ask them to do. A space groups conversations, and
     * conversations are what this destination is for.
     */
    sections: [
      {
        id: "main",
        label: "",
        items: [
          { view: "chat", label: "Chat", icon: MessageSquare },
          { view: "spaces", label: "Spaces", icon: Layers },
          { view: "agents", label: "Agents", icon: Bot },
          { view: "skills", label: "Skills", icon: Sparkles },
        ],
      },
    ],
  },
  /**
   * What needs you. The approval queue used to be the first half of a page
   * called Activity, reachable only through the Settings menu — four clicks
   * from the thing that was actually blocked on a human. It is a rail
   * destination now, second from the top, and the count of parked requests
   * rides its rail icon as the shell's only numeric badge.
   */
  {
    id: "inbox",
    label: "Inbox",
    icon: Inbox,
    surface: "rail",
    sections: [
      {
        id: "main",
        label: "",
        items: [
          { view: "activity", label: "Inbox", icon: Inbox },
          // The ledger of standing grants beside the queue that writes them:
          // an "always allow" ticked on an approval card lands here, and here
          // is where it is taken back. The org ceilings render on the same
          // page — the rule that denies you belongs next to the rules you
          // wrote yourself.
          { view: "policies", label: "Rules", icon: ShieldCheck },
        ],
      },
    ],
  },
  /**
   * "Library", because that is what it holds — and held under headings,
   * because a flat strip of seven tabs answers "where is my stuff" with a
   * lineup rather than a shelf.
   */
  {
    id: "files",
    label: "Library",
    icon: LibraryBig,
    surface: "rail",
    sections: [
      {
        id: "main",
        label: "",
        items: [
          { view: "documents", label: "Documents", icon: FileText },
          { view: "projects", label: "Projects", icon: Braces },
        ],
      },
      {
        /**
         * One destination, because a list is a board with one column and that
         * is an implementation detail nobody should have to know to find
         * their checklist. Two entries used to sit here, which made a list
         * *teleport* to the other row the moment it grew a second column;
         * now the object stays put in one listing and only its glyph changes.
         */
        id: "boards",
        label: "Boards & todos",
        items: [{ view: "boards", label: "Boards & todos", icon: KanbanSquare }],
      },
      {
        /**
         * Data beside the things drawn from it. Databases used to live behind
         * the Settings menu while Dashboards lived here — the connect-data →
         * chart-it trek crossed the whole shell. Now the pipeline reads top to
         * bottom: connect or upload, dataset, dashboard.
         */
        id: "data",
        label: "Data",
        items: [
          { view: "datasets", label: "Datasets", icon: Table2 },
          { view: "data", label: "Databases", icon: Database },
        ],
      },
      {
        id: "dashboards",
        label: "Dashboards",
        items: [
          { view: "dashboards", label: "Dashboards", icon: BarChart3 },
          /**
           * Apps are programs the sandbox builds, publishes and rolls back.
           * They shared a page with Dashboards once, which made "Add
           * dashboard" secretly build an app. Same heading, two entries, two
           * words that each mean one thing.
           */
          { view: "apps", label: "Apps", icon: LayoutGrid },
        ],
      },
      {
        /**
         * Inside Library rather than on the rail: with section headings always
         * visible, Sources/Memory/Graph are nearer than their old rail seat
         * left them — one click from anywhere and on screen the whole time you
         * are in Library. The rail seat existed to keep them out of a Settings
         * menu, and that threat is gone with the menu.
         */
        id: "knowledge",
        label: "Knowledge",
        items: [
          { view: "sources", label: "Sources", icon: Library },
          { view: "memory", label: "Memory", icon: Brain },
          { view: "graph", label: "Graph", icon: Network },
        ],
      },
    ],
  },
  /**
   * On the rail, and the argument had two sides.
   *
   * Against: the rail is deliberately short, and a workflow is a thing you
   * *make*. But nobody looking for automation looks under "Library", and only
   * half of what this surface does is making. The other half is operating —
   * watching a run, and answering the approval an unattended run parked on.
   *
   * The group is "Automations" — the idea — and its entries are the two
   * kinds: Workflows (a sentence compiled to a reviewable graph) and
   * Schedules (a recurring prompt on a timer).
   */
  {
    id: "workflows",
    label: "Automations",
    icon: Workflow,
    surface: "rail",
    sections: [
      {
        id: "main",
        label: "",
        items: [
          { view: "workflows", label: "Workflows", icon: Workflow },
          { view: "crons", label: "Schedules", icon: Clock },
        ],
      },
    ],
  },
  {
    id: "connections",
    label: "Connections",
    icon: Plug,
    surface: "settings",
    sections: [
      {
        id: "main",
        label: "",
        items: [
          { view: "mcp", label: "MCP", icon: Blocks },
          // Beside MCP because it is the same kind of surface: registering
          // tools the agent may call. Databases left for Library → Data,
          // where the datasets they feed live.
          { view: "sandbox-tools", label: "Sandbox tools", icon: Terminal },
          { view: "integrations", label: "Integrations", icon: Plug },
        ],
      },
    ],
  },
  {
    id: "admin",
    label: "Admin",
    icon: ShieldCheck,
    surface: "settings",
    sections: [
      { id: "main", label: "", items: [{ view: "admin", label: "Admin", icon: ShieldCheck }] },
    ],
  },
];

export const NAV_GROUPS: NavGroup[] = GROUP_SPECS.map((group) => ({
  ...group,
  items: group.sections.flatMap((section) => section.items),
}));

/** The rail, in order. Derived so a group cannot be on the rail and nowhere else. */
export const RAIL_GROUPS = NAV_GROUPS.filter((group) => group.surface === "rail");

/** What the workspace-settings menu offers, in order. */
export const SETTINGS_GROUPS = NAV_GROUPS.filter((group) => group.surface === "settings");

const ITEM_OF_VIEW = new Map<View, NavItem>(
  NAV_GROUPS.flatMap((group) => group.items.map((item) => [item.view, item] as const)),
);

const GROUP_OF_VIEW = new Map<View, GroupId>(
  NAV_GROUPS.flatMap((group) => group.items.map((item) => [item.view, group.id] as const)),
);

export function groupForView(view: View): NavGroup {
  const id = GROUP_OF_VIEW.get(view);
  // Unreachable while the union and NAV_GROUPS agree — navigation.test.ts pins
  // that — but a missing group must not blank the shell.
  return NAV_GROUPS.find((group) => group.id === id) ?? NAV_GROUPS[0];
}

/** Each group's landing view, before the user has visited anything inside it. */
export const DEFAULT_GROUP_VIEW = Object.fromEntries(
  NAV_GROUPS.map((group) => [group.id, group.items[0].view]),
) as Record<GroupId, View>;

export type CreateActionId =
  | "document"
  | "project"
  | "latex"
  | "board"
  | "app"
  | "workflow";

export type CreateAction = {
  id: CreateActionId;
  /** Singular, because picking this makes exactly one of them. */
  label: string;
  /**
   * The label as it reads mid-sentence — "New …", "Create …". Spelled out
   * rather than lowercasing `label`, because "LaTeX" is a wordmark and
   * toLowerCase() would turn the one entry this menu has to name precisely
   * into "latex".
   */
  noun: string;
  /** Where the new thing opens. Also what ties the action to the nav model. */
  view: View;
  icon: LucideIcon;
  /**
   * What to ask for before creating, or "" for the things that name themselves
   * later: an app collects its name, datasets and prompt in the editor it
   * opens, and a workflow is named by the compiler from the sentence it was
   * asked for.
   */
  prompt: string;
};

/**
 * The Create menu. Every entry names a view from NAV_GROUPS and borrows its
 * icon, so an action can never point at something the rest of the navigation
 * cannot reach, and the two surfaces cannot drift into different icons.
 *
 * "LaTeX document" is a Project, not a Document, and it is here because that
 * was not discoverable: a user looking for LaTeX found the *document* format
 * of the same name, which renders KaTeX maths and produces no PDF, and
 * reported the TeX compiler as broken. In this menu the word now means one
 * thing — TeX in, PDF out.
 *
 * There is no "Dashboard" here, and that is the fix for a lie: the entry that
 * said "Dashboard" opened the app editor and built a sandbox program.
 * Dashboards are written by the agent during a conversation — ask for a chart
 * — so the menu offers the thing the editor actually makes, an App.
 *
 * There is no "Folder" here, and that is not an oversight. Every entry in this
 * menu makes something with contents; a folder is only the place contents go,
 * and the one thing worth saying when you make one — *which folder it goes in*
 * — is a question a menu in the top-right corner has no way to ask. It is made
 * in the tree, where the answer is whatever you were pointing at. "Sandbox" is
 * gone for the harder reason: it was never a thing you make, it was a machine
 * you were being asked to operate.
 */
export const CREATE_ACTIONS: CreateAction[] = (
  [
    {
      id: "document",
      label: "Document",
      noun: "document",
      view: "documents",
      prompt: "Document title",
    },
    { id: "project", label: "Project", noun: "project", view: "projects", prompt: "Project name" },
    {
      id: "latex",
      label: "LaTeX document",
      noun: "LaTeX document",
      view: "projects",
      prompt: "Document name",
    },
    { id: "board", label: "Board", noun: "board", view: "boards", prompt: "Board name" },
    {
      id: "app",
      label: "App",
      noun: "app",
      view: "apps",
      prompt: "",
    },
    // Nothing is asked for here, for the same reason an app asks for
    // nothing: the sentence *is* the workflow. A name typed now would be
    // thrown away, because the compiler names the automation from the ask.
    {
      id: "workflow",
      label: "Workflow",
      noun: "workflow",
      view: "workflows",
      prompt: "",
    },
  ] as const
).map((action) => {
  const item = ITEM_OF_VIEW.get(action.view);
  if (!item) {
    // Only reachable if someone deletes a view from NAV_GROUPS but leaves the
    // action behind — which is exactly the unreachable-thing bug this table
    // exists to prevent, so it fails loudly rather than rendering a blank row.
    throw new Error(`Create action "${action.id}" targets a view no group lists`);
  }
  return { ...action, icon: item.icon };
});
