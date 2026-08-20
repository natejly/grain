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
  ListChecks,
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
 * The rail answers "what am I trying to do", not "which subsystem owns this",
 * and its groups are named for what they actually hold. Two names carried lies
 * for a while and both are gone: "Files" held boards, lists and dashboards —
 * none of which are files — and is now Library; "Automations" named the cron
 * tab while its sibling group was called Workflows, so the same idea had two
 * names a click apart. Schedules is the cron surface, Automations is the group
 * that holds it and Workflows both.
 *
 * Inbox is on the rail because approvals are the product's primary work
 * object, not an audit artifact. An approval that has been waiting since 3am
 * was hidden behind a menu labelled "Settings", where a badge on a gear read
 * as configuration noise. It carries the shell's only numeric badge.
 *
 * Sandbox left for a different reason, and it is the sharper one: a sandbox is
 * a *capability*, not a destination. You do not visit it any more than you
 * visit the database — you ask for a chart and you get one, and the figure
 * appears on the tool card in the conversation that asked.
 */
export type GroupId =
  | "chat"
  | "inbox"
  | "files"
  | "workflows"
  | "knowledge"
  | "connections"
  | "admin";

/**
 * Where a group is reached from. "rail" is the left sidebar — the places you
 * work. "settings" is the top-right workspace-settings menu — the places you
 * configure, which are visited rarely and do not deserve permanent rail space.
 * Nothing that *waits on a person* may live behind "settings".
 *
 * Both surfaces read this one list, so a group cannot appear on neither.
 */
export type NavSurface = "rail" | "settings";

export type NavItem = {
  view: View;
  /** Short label for the sidebar/tab strip; PAGE_TITLES carries the long form. */
  label: string;
  icon: LucideIcon;
};

export type NavGroup = {
  id: GroupId;
  label: string;
  icon: LucideIcon;
  surface: NavSurface;
  items: NavItem[];
};

export const NAV_GROUPS: NavGroup[] = [
  {
    id: "chat",
    label: "Chat",
    icon: MessageSquare,
    surface: "rail",
    /**
     * Agents live beside Chat, not in settings: an agent is who you are
     * talking to, so the place you author one is a tab away from the place
     * you use one. A skill is the same argument in the other dimension — not
     * who answers but what you ask them to do — so it sits alongside, authored
     * a tab from the composer that invokes it with "/".
     */
    items: [
      { view: "chat", label: "Chat", icon: MessageSquare },
      /**
       * A space groups threads under standing context — instructions,
       * knowledge files, a memory shelf of its own. It lives beside Chat, not
       * in Library, because it groups conversations, and conversations are
       * what this rail group is for. (Chat stays first: DEFAULT_GROUP_VIEW
       * reads items[0].)
       */
      { view: "spaces", label: "Spaces", icon: Layers },
      { view: "agents", label: "Agents", icon: Bot },
      { view: "skills", label: "Skills", icon: Sparkles },
    ],
  },
  /**
   * What needs you. The approval queue used to be the first half of a page
   * called Activity, reachable only through the Settings menu — four clicks
   * from the thing that was actually blocked on a human. It is a rail
   * destination now, second from the top, and the count of parked requests
   * rides its rail row as the shell's only numeric badge.
   */
  {
    id: "inbox",
    label: "Inbox",
    icon: Inbox,
    surface: "rail",
    items: [{ view: "activity", label: "Inbox", icon: Inbox }],
  },
  /**
   * "Library", because that is what it holds: documents, projects, boards,
   * lists, dashboards, apps. Its previous name — "Files" — described one tab
   * and mislabelled the rest, and doubled with that tab's own label so the
   * breadcrumb read "Files / Files".
   */
  {
    id: "files",
    label: "Library",
    icon: LibraryBig,
    surface: "rail",
    items: [
      { view: "documents", label: "Documents", icon: FileText },
      { view: "projects", label: "Projects", icon: Braces },
      { view: "boards", label: "Boards", icon: KanbanSquare },
      /**
       * Beside Boards rather than inside them, because a list is a board with
       * one column and that is an implementation detail nobody should have to
       * know to find their checklist. A tab of its own is also what makes the
       * graduation legible in the other direction: add a second column to a
       * list and it stops appearing here and starts appearing there, same id,
       * same items, ticks intact.
       */
      { view: "todos", label: "Lists", icon: ListChecks },
      /**
       * Data before the things drawn from it. A dataset had no home at all —
       * created as a side effect of uploading a CSV, findable only inside the
       * app editor — which left "what data does this workspace hold" with no
       * answer a user could reach.
       */
      { view: "datasets", label: "Datasets", icon: Table2 },
      { view: "dashboards", label: "Dashboards", icon: BarChart3 },
      /**
       * Apps are programs the sandbox builds, publishes and rolls back —
       * releases, visibility, an iframe of their own. They shared a page with
       * Dashboards for a while, which made "Add dashboard" secretly build an
       * app and left both kinds of thing harder to name. Two tabs, two things.
       */
      { view: "apps", label: "Apps", icon: LayoutGrid },
    ],
  },
  {
    id: "knowledge",
    label: "Knowledge",
    icon: Library,
    surface: "rail",
    items: [
      { view: "sources", label: "Sources", icon: Library },
      { view: "memory", label: "Memory", icon: Brain },
      { view: "graph", label: "Graph", icon: Network },
    ],
  },
  /**
   * On the rail, and the argument had two sides.
   *
   * Against: the rail is deliberately short, and a workflow is a thing you
   * *make*, which would put it among Library's siblings. But nobody looking
   * for automation looks under "Library", and only half of what this surface
   * does is making. The other half is operating — watching a run, and
   * answering the approval an unattended run parked on.
   *
   * The group is "Automations" — the idea — and its tabs are the two kinds:
   * Workflows (a sentence compiled to a reviewable graph) and Schedules (a
   * recurring prompt on a timer). The cron tab was itself called "Automations"
   * for a while, which gave the same word two homes; a cron is a schedule, so
   * now it says so.
   */
  {
    id: "workflows",
    label: "Automations",
    icon: Workflow,
    surface: "rail",
    items: [
      { view: "workflows", label: "Workflows", icon: Workflow },
      { view: "crons", label: "Schedules", icon: Clock },
    ],
  },
  {
    id: "connections",
    label: "Connections",
    icon: Plug,
    surface: "settings",
    items: [
      { view: "data", label: "Databases", icon: Database },
      { view: "mcp", label: "MCP", icon: Blocks },
      // Beside MCP because it is the same kind of surface: registering tools the
      // agent may call. Not on the rail — you configure it rarely and on
      // purpose, and it is not a machine you operate.
      { view: "sandbox-tools", label: "Sandbox tools", icon: Terminal },
      { view: "integrations", label: "Integrations", icon: Plug },
    ],
  },
  {
    id: "admin",
    label: "Admin",
    icon: ShieldCheck,
    surface: "settings",
    items: [{ view: "admin", label: "Admin", icon: ShieldCheck }],
  },
];

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
