import type { DashboardSubscription } from "@workspace/api-client";

/**
 * The pure half of the subscribe popover: the schedule presets it offers, the
 * words a stored schedule reads as, and which subscriptions belong to the
 * dashboard being subscribed. React-free so `tests/subscription-format.test.ts`
 * can pin the semantics without a DOM.
 */

export type SchedulePreset = {
  id: string;
  label: string;
  cron: string;
};

/**
 * The two shapes a snapshot mail is actually asked for. Presets rather than a
 * cron textarea because this popover sits on a dashboard row, not in an
 * automations editor — someone wanting "every third Tuesday" has the Schedules
 * view; someone here wants their morning number.
 */
export const SUBSCRIPTION_PRESETS: SchedulePreset[] = [
  { id: "daily", label: "Every day at 9:00", cron: "0 9 * * *" },
  { id: "weekly", label: "Mondays at 9:00", cron: "0 9 * * 1" },
];

/**
 * A stored schedule, in the preset's words when it matches one, and honestly
 * as `cron · zone` when it does not (a subscription made by the API can carry
 * any cron). The zone is always shown: "9:00" without a timezone is a number
 * pretending to be a time.
 */
export function describeSubscriptionSchedule(
  scheduleCron: string,
  scheduleTimezone: string,
): string {
  const preset = SUBSCRIPTION_PRESETS.find((item) => item.cron === scheduleCron);
  const zone = scheduleTimezone || "UTC";
  if (preset) return `${preset.label} · ${zone}`;
  return `${scheduleCron} · ${zone}`;
}

/**
 * The subscriptions about one dashboard, newest first. The API returns every
 * row the caller may see (their own, or all of them for an owner), so the
 * popover — which is always about one dashboard — filters here.
 */
export function subscriptionsFor(
  subscriptions: DashboardSubscription[],
  dashboardId: string,
): DashboardSubscription[] {
  return subscriptions
    .filter((subscription) => subscription.dashboard_id === dashboardId)
    .sort((a, b) => b.created_at.localeCompare(a.created_at));
}
