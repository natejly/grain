import type { Monitor, MonitorComparator } from "@workspace/api-client";
import type { ScheduleNote } from "./workflow-format";

/**
 * Pure copy for the Monitors view and the Inbox's Alerts tab — React-free, so
 * the words a monitor uses about itself are pinned by unit tests rather than
 * re-derived in three components.
 *
 * The vocabulary is deliberately about *crossing*, not being: a monitor alerts
 * on the ok→tripped edge, so "rises above" is the honest verb — "is above"
 * would describe a level the monitor deliberately stays quiet about while it
 * persists.
 */

/** The comparator as the sentence "alerts when the value … the threshold". */
export const COMPARATOR_LABELS: Record<MonitorComparator, string> = {
  gt: "rises above",
  lt: "falls below",
  gte: "reaches",
  lte: "drops to",
};

/** What the monitor watches: its first metric's label, which is the value the
 * evaluation reads. "value" only for a malformed query the server would have
 * refused — a caller should never see it. */
export function metricLabel(monitor: Monitor): string {
  return monitor.query.metrics?.[0]?.label || "value";
}

/** One sentence of intent: what trips this monitor. */
export function thresholdCopy(monitor: Monitor): string {
  return `Alerts when ${metricLabel(monitor)} ${COMPARATOR_LABELS[monitor.comparator]} ${monitor.threshold}`;
}

/**
 * The state chip's words. "" is "not evaluated yet" — an honest unknown, never
 * dressed up as OK: a monitor that has not looked cannot vouch for anything.
 */
export function stateLabel(state: string): string {
  if (state === "tripped") return "Tripped";
  if (state === "ok") return "Within threshold";
  return "Not evaluated yet";
}

/** The chip tone for each state: warn for tripped, live for ok, muted unknown. */
export function stateTone(state: string): "warn" | "live" | "" {
  if (state === "tripped") return "warn";
  if (state === "ok") return "live";
  return "";
}

/** "Last value 60" — from the stored JSON, or "" before the first evaluation
 * (and for a stored value that does not parse, rather than rendering "NaN"). */
export function lastValueCopy(monitor: Monitor): string {
  if (!monitor.last_value_json) return "";
  try {
    const value: unknown = JSON.parse(monitor.last_value_json);
    if (typeof value !== "number") return "";
    return `Last value ${value}`;
  } catch {
    return "";
  }
}

/**
 * The truth about when this monitor evaluates, in the Schedules view's tone —
 * branch for branch the discipline `describeCronSchedule` carries: `enabled`
 * is believed before any armed-state (a switched-off monitor is evaluated by
 * nobody however armed the ticker is), an unarmed ticker makes the schedule a
 * recorded intention rather than a promise, and an unanswered probe is "we do
 * not know", never "yes".
 */
export function describeMonitorSchedule(
  monitor: Monitor,
  schedulingEnabled: boolean | null,
): ScheduleNote {
  const when = `${monitor.schedule_cron} · ${monitor.schedule_timezone}`;
  if (!monitor.enabled) {
    return {
      tone: "warn",
      headline: "Disabled — nothing will evaluate it",
      detail: `Enable this monitor to evaluate it on ${when}.`,
    };
  }
  if (schedulingEnabled === false) {
    return {
      tone: "warn",
      headline: "Schedule stored, but nothing will fire it",
      detail:
        `This deployment has no cron ticker configured, so ${when} is a ` +
        "recorded intention and not a promise. Until an operator arms the " +
        "tick endpoint, this monitor only evaluates when you press Check now.",
    };
  }
  if (schedulingEnabled === null) {
    return {
      tone: "warn",
      headline: "Schedule stored; the scheduler did not answer",
      detail:
        `${when}. We could not reach the tick endpoint to find out whether ` +
        "anything dispatches it, so treat this as unscheduled until it answers.",
    };
  }
  return {
    tone: "live",
    headline: `Checks on ${when}`,
    detail: monitor.last_dispatched_at
      ? `Scheduling is configured. Last checked ${new Date(
          monitor.last_dispatched_at,
        ).toLocaleString()}.`
      : "Scheduling is configured. It has not checked yet.",
  };
}
