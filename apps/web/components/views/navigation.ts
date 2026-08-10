import {
  Activity,
  BarChart3,
  Blocks,
  Braces,
  Brain,
  Database,
  FileText,
  KanbanSquare,
  Library,
  MessageSquare,
  Network,
  Plug,
  ShieldCheck,
  Terminal,
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
 * the thing; the *documents* that used to hide behind it are a destination in
 * their own right, and they took their siblings' tab strip with them. Knowledge
 * stays on the rail on purpose: a user who could not find their memories is not
 * helped by moving them further away.
 */
export type GroupId =
  | "chat"
  | "documents"
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
    items: [{ view: "chat", label: "Chat", icon: MessageSquare }],
  },
  {
    id: "documents",
    label: "Documents",
    icon: FileText,
    surface: "rail",
    items: [
      { view: "documents", label: "Documents", icon: FileText },
      { view: "projects", label: "Projects", icon: Braces },
      { view: "sandbox", label: "Sandbox", icon: Terminal },
      { view: "boards", label: "Boards", icon: KanbanSquare },
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
  | "sandbox"
  | "board"
  | "dashboard";

export type CreateAction = {
  id: CreateActionId;
  /** Singular, because picking this makes exactly one of them. */
  label: string;
  /** Where the new thing opens. Also what ties the action to the nav model. */
  view: View;
  icon: LucideIcon;
  /**
   * What to ask for before creating, or "" for the things that need nothing
   * said first: a sandbox is a machine, and a dashboard collects its name,
   * datasets and prompt in the editor it opens.
   */
  prompt: string;
};

/**
 * The Create menu. Every entry names a view from NAV_GROUPS and borrows its
 * icon, so an action can never point at something the rest of the navigation
 * cannot reach, and the two surfaces cannot drift into different icons.
 */
export const CREATE_ACTIONS: CreateAction[] = (
  [
    { id: "document", label: "Document", view: "documents", prompt: "Document title" },
    { id: "project", label: "Project", view: "projects", prompt: "Project name" },
    { id: "sandbox", label: "Sandbox", view: "sandbox", prompt: "" },
    { id: "board", label: "Board", view: "boards", prompt: "Board name" },
    { id: "dashboard", label: "Dashboard", view: "dashboards", prompt: "" },
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
