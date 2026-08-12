import {
  Activity,
  BarChart3,
  Blocks,
  Bot,
  Braces,
  Brain,
  Database,
  FileText,
  KanbanSquare,
  Library,
  ListChecks,
  MessageSquare,
  Network,
  Plug,
  ShieldCheck,
  Sparkles,
  Workflow,
  type LucideIcon,
} from "lucide-react";
import type { View } from "./shared";

/**
 * One table describes the whole of navigation, because a view that is missing
 * from it is a view with no way to reach it — and the shell would still render.
 *
 * The sidebar answers "what am I trying to do", not "which subsystem owns
 * this". Eleven flat entries required a user to know the architecture before
 * they could find anything; a group's siblings are reachable from a tab strip
 * once you are inside it, and the first item of a group is where it lands.
 *
 * What changed here is that "Create" stopped being a place. Creating is an
 * action, so it left the rail for a menu in the top right that actually makes
 * the thing; the *files* that used to hide behind it are a destination in their
 * own right, and they took their siblings' tab strip with them. Knowledge stays
 * on the rail on purpose: a user who could not find their memories is not
 * helped by moving them further away.
 *
 * Sandbox left for a different reason, and it is the sharper one: a sandbox is
 * a *capability*, not a destination. You do not visit it any more than you
 * visit the database — you ask for a chart and you get one, and the figure
 * appears on the tool card in the conversation that asked. A rail entry for it
 * was inviting users to go and operate a machine, which is the agent's job; it
 * also invited them, whenever SANDBOX_ENABLED was false, to start a machine
 * that 502s. The service, its tools and its API are untouched.
 */
export type GroupId =
  | "chat"
  | "files"
  | "workflows"
  | "knowledge"
  | "connections"
  | "activity"
  | "admin";

/**
 * Where a group is reached from. "rail" is the left sidebar — the places you
 * work. "settings" is the top-right menu — the places you configure and audit,
 * which are visited rarely and do not deserve permanent rail space.
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
      { view: "agents", label: "Agents", icon: Bot },
      { view: "skills", label: "Skills", icon: Sparkles },
    ],
  },
  /**
   * "Files", not "Documents". The group has held Projects, Boards and
   * Dashboards for as long as it has existed, so its old name described one of
   * its four tabs and mislabelled the other three — and now that the first tab
   * is a folder tree rather than a flat list, "Files" is also what the thing
   * itself is.
   */
  {
    id: "files",
    label: "Files",
    icon: FileText,
    surface: "rail",
    items: [
      { view: "documents", label: "Files", icon: FileText },
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
      { view: "dashboards", label: "Dashboards", icon: BarChart3 },
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
   * *make*, which would put it among Files' siblings. But nobody looking for
   * automation looks under "Files", and only half of what this surface
   * does is making. The other half is operating — watching a run, and answering
   * the approval an unattended run parked on. Settings is where you go rarely
   * and on purpose; an approval that has been waiting since 3am is the opposite
   * of that. So: a place you work, appended rather than inserted, because the
   * three that were already here should not move for a fourth.
   */
  {
    id: "workflows",
    label: "Workflows",
    icon: Workflow,
    surface: "rail",
    items: [{ view: "workflows", label: "Workflows", icon: Workflow }],
  },
  {
    id: "connections",
    label: "Connections",
    icon: Plug,
    surface: "settings",
    items: [
      { view: "data", label: "Databases", icon: Database },
      { view: "mcp", label: "MCP", icon: Blocks },
      { view: "integrations", label: "Integrations", icon: Plug },
    ],
  },
  {
    id: "activity",
    label: "Activity",
    icon: Activity,
    surface: "settings",
    items: [{ view: "activity", label: "Activity", icon: Activity }],
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

/** What the Settings menu offers, in order. */
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
  | "dashboard"
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
   * later: a dashboard collects its name, datasets and prompt in the editor it
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
      id: "dashboard",
      label: "Dashboard",
      noun: "dashboard",
      view: "dashboards",
      prompt: "",
    },
    // Nothing is asked for here, for the same reason a dashboard asks for
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
