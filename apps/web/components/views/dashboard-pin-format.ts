import type { AgentToolCall, Dashboard } from "@workspace/api-client";

/** Tool calls that leave a dashboard behind — a thing that can be pinned. */
export const DASHBOARD_TOOLS = [
  "create_dashboard",
  "update_dashboard",
  "bind_dashboard_template",
];

/**
 * The composer seed for a chart that is only a picture. A sandbox PNG carries
 * no dataset or query, so there is nothing to pin — the honest offer is to ask
 * the agent for the pinnable version, in the user's own voice so they can
 * point at the dataset before sending. Named for the call that drew the chart,
 * so the sentence says which chart it means in a turn that drew several.
 */
export function makeDashboardSeed(callName: string): string {
  return `Turn the chart the ${callName} call above drew into a dashboard I can pin: `;
}

/**
 * Which dashboard a dashboard tool call left behind, or null when it cannot
 * be known.
 *
 * Only for a call that succeeded: a proposed or denied `create_dashboard`
 * made nothing, and a pin button under a card that was refused would offer to
 * pin a dashboard that does not exist. Nor for one whose result is an error
 * line — the executors report "Error: …" with the status still "succeeded",
 * and a name-clash error names a dashboard the call never touched. The id is
 * read from the success line the server writes — every one carries
 * "(id <uuid>)" — and resolved against the workspace's dashboard list, which
 * the shell refetches when the turn settles. There is deliberately no
 * name-in-arguments fallback: a name can match a pre-existing dashboard the
 * call had nothing to do with, and pinning the wrong dashboard to someone's
 * home screen is worse than showing no button.
 */
export function dashboardForCall(
  call: AgentToolCall,
  dashboards: Dashboard[],
): Dashboard | null {
  if (!DASHBOARD_TOOLS.includes(call.name) || call.status !== "succeeded") return null;
  if (call.result_preview.startsWith("Error")) return null;
  const id = /\(id ([0-9a-fA-F-]{36})\)/.exec(call.result_preview)?.[1];
  if (!id) return null;
  return dashboards.find((dashboard) => dashboard.id === id) ?? null;
}

/** Whether a call drew a picture of a chart — pixels, with no query behind them. */
export function hasChartImage(call: AgentToolCall): boolean {
  return call.artifacts.some((artifact) => artifact.mime.startsWith("image/"));
}
