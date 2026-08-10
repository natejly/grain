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
  SquarePen,
  Terminal,
  type LucideIcon,
} from "lucide-react";
import type { View } from "./shared";

/**
 * The sidebar answers "what am I trying to do", not "which subsystem owns
 * this". Eleven flat entries required a user to know the architecture before
 * they could find anything; these five groups are the whole navigation, and a
 * group's siblings are reachable from a tab strip once you are inside it.
 *
 * The first item of a group is its default landing view.
 */
export type GroupId = "chat" | "create" | "knowledge" | "connections" | "activity";

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
  items: NavItem[];
};

export const NAV_GROUPS: NavGroup[] = [
  {
    id: "chat",
    label: "Chat",
    icon: MessageSquare,
    items: [{ view: "chat", label: "Chat", icon: MessageSquare }],
  },
  {
    id: "create",
    label: "Create",
    icon: SquarePen,
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
    items: [{ view: "activity", label: "Activity", icon: Activity }],
  },
];

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
