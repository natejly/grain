import type {
  DatasetMetric,
  DatasetQuery,
  Monitor,
  MonitorComparator,
  MonitorUpdateInput,
} from "@workspace/api-client";
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

/** The one aggregation the Monitors form can express. */
export type MonitorFormOperation = "count" | "sum" | "avg" | "min" | "max";

/** The one metric the form builds: what the evaluation will read. The label
 * is derived, never free-typed — `formMetric` relies on recognizing it. */
export function buildMetric(
  operation: MonitorFormOperation,
  field: string,
): DatasetMetric {
  if (operation === "count") return { operation, field: null, label: "count" };
  return { operation, field, label: `${operation}_${field}` };
}

/**
 * Read a stored query back into the form's `{operation, field}` shape — or
 * null when the query carries anything the form cannot express (filters,
 * grouping, ordering, extra metrics, a hand-written label). Null means the
 * edit view must keep the stored query verbatim: rebuilding it from the form
 * would silently drop the parts the form has no controls for.
 */
export function formMetric(
  query: DatasetQuery,
): { operation: MonitorFormOperation; field: string } | null {
  const metrics = query.metrics ?? [];
  const metric = metrics[0];
  if (
    (query.filters?.length ?? 0) > 0 ||
    query.group_by ||
    query.order_by ||
    metrics.length !== 1 ||
    metric === undefined
  ) {
    return null;
  }
  const field = metric.field ?? "";
  const rebuilt = buildMetric(metric.operation, field);
  if (metric.label !== rebuilt.label || field !== (rebuilt.field ?? "")) {
    return null;
  }
  return { operation: metric.operation, field };
}

/** What the edit form holds; `query`/`dataset_id` null means "untouched". */
export type MonitorDraft = {
  name: string;
  comparator: MonitorComparator;
  threshold: number;
  schedule_cron: string;
  schedule_timezone: string;
  dataset_id: string | null;
  query: DatasetQuery | null;
};

/**
 * The PUT body for an edit: ONLY the fields that actually changed. The server
 * resets a monitor's edge state whenever a definition field (dataset, query,
 * comparator, threshold) is among the sent fields — by design, so the first
 * crossing after a redefine alerts — which is exactly why an unchanged field
 * must be omitted rather than re-sent: saving an untouched form must not
 * quietly re-arm a tripped monitor.
 */
export function monitorUpdatePayload(
  monitor: Monitor,
  draft: MonitorDraft,
): MonitorUpdateInput {
  const payload: MonitorUpdateInput = {};
  if (draft.name !== monitor.name) payload.name = draft.name;
  if (draft.comparator !== monitor.comparator) payload.comparator = draft.comparator;
  if (draft.threshold !== monitor.threshold) payload.threshold = draft.threshold;
  if (draft.schedule_cron !== monitor.schedule_cron) {
    payload.schedule_cron = draft.schedule_cron;
  }
  if (draft.schedule_timezone !== monitor.schedule_timezone) {
    payload.schedule_timezone = draft.schedule_timezone;
  }
  if (draft.dataset_id !== null && draft.dataset_id !== monitor.dataset_id) {
    payload.dataset_id = draft.dataset_id;
  }
  if (draft.query !== null) payload.query = draft.query;
  return payload;
}

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
