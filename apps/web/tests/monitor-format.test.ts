import type { Monitor } from "@workspace/api-client";
import { describe, expect, it } from "vitest";
import {
  buildMetric,
  describeMonitorSchedule,
  formMetric,
  lastValueCopy,
  metricLabel,
  monitorUpdatePayload,
  stateLabel,
  stateTone,
  thresholdCopy,
} from "../components/views/monitor-format";

/**
 * The words a monitor uses about itself, pinned. Two disciplines meet here:
 *
 * The SCHEDULE copy carries the ADR-0007 rule the Workflows and Schedules
 * views already obey — a stored cron is an intention, only an armed ticker
 * makes it a promise, and a branch that cannot promise a dispatch says so in
 * the headline — plus the sibling rule that `enabled` is believed before any
 * armed-state, because an operator who switched a monitor off must never read
 * "Checks on …".
 *
 * The STATE copy has one rule of its own: "" (never evaluated) is an honest
 * unknown. A monitor that has not looked cannot vouch for anything, so the
 * unknown state must render as "not evaluated yet" and never borrow OK's
 * label or tone.
 */

const monitor: Monitor = {
  id: "m1",
  name: "Revenue floor",
  dataset_id: "d1",
  query: {
    metrics: [{ field: "amount", operation: "sum", label: "sum_amount" }],
    limit: 1,
  },
  comparator: "lt",
  threshold: 100,
  schedule_cron: "0 9 * * 1",
  schedule_timezone: "Europe/London",
  enabled: true,
  last_state: "",
  last_value_json: "",
  last_dispatched_at: null,
  created_at: "2026-08-01T00:00:00Z",
  updated_at: "2026-08-01T00:00:00Z",
};

describe("thresholdCopy", () => {
  it("says what is watched and which crossing alerts, as one sentence", () => {
    // "falls below", not "is below": the monitor alerts on the edge, and the
    // copy must not promise a page for a level it deliberately stays quiet
    // about while it persists.
    expect(thresholdCopy(monitor)).toBe("Alerts when sum_amount falls below 100");
    expect(thresholdCopy({ ...monitor, comparator: "gt", threshold: 5 })).toBe(
      "Alerts when sum_amount rises above 5",
    );
  });

  it("reads the FIRST metric's label — the value the evaluation actually uses", () => {
    const twoMetrics: Monitor = {
      ...monitor,
      query: {
        metrics: [
          { operation: "count", field: null, label: "rows" },
          { field: "amount", operation: "sum", label: "sum_amount" },
        ],
      },
    };
    expect(metricLabel(twoMetrics)).toBe("rows");
  });
});

describe("stateLabel / stateTone", () => {
  it("treats never-evaluated as an honest unknown, not as OK", () => {
    expect(stateLabel("")).toBe("Not evaluated yet");
    expect(stateTone("")).toBe("");
  });

  it("gives tripped the warn tone and ok the live one", () => {
    expect(stateLabel("tripped")).toBe("Tripped");
    expect(stateTone("tripped")).toBe("warn");
    expect(stateLabel("ok")).toBe("Within threshold");
    expect(stateTone("ok")).toBe("live");
  });
});

describe("lastValueCopy", () => {
  it("is silent before the first evaluation rather than inventing a zero", () => {
    expect(lastValueCopy(monitor)).toBe("");
  });

  it("reads the stored JSON number back as a sentence", () => {
    expect(lastValueCopy({ ...monitor, last_value_json: "60" })).toBe("Last value 60");
  });

  it("stays silent on an unparseable stored value instead of rendering NaN", () => {
    expect(lastValueCopy({ ...monitor, last_value_json: "not json" })).toBe("");
  });
});

describe("describeMonitorSchedule", () => {
  it("believes disabled before any armed-state, whatever the ticker is doing", () => {
    // An operator who turned the monitor off must never read "Checks on …"
    // because the deployment happens to have a secret set.
    const note = describeMonitorSchedule({ ...monitor, enabled: false }, true);
    expect(note.tone).toBe("warn");
    expect(note.headline).toBe("Disabled — nothing will evaluate it");
  });

  it("calls an unarmed deployment's schedule a stored intention, in the headline", () => {
    const note = describeMonitorSchedule(monitor, false);
    expect(note.tone).toBe("warn");
    expect(note.headline).toBe("Schedule stored, but nothing will fire it");
    expect(note.detail).toContain("0 9 * * 1 · Europe/London");
  });

  it("treats an unanswered probe as unknown, never as yes", () => {
    const note = describeMonitorSchedule(monitor, null);
    expect(note.tone).toBe("warn");
    expect(note.headline).toBe("Schedule stored; the scheduler did not answer");
  });

  it("promises the schedule only when the ticker is armed, and says if it has fired", () => {
    const fresh = describeMonitorSchedule(monitor, true);
    expect(fresh.tone).toBe("live");
    expect(fresh.headline).toBe("Checks on 0 9 * * 1 · Europe/London");
    expect(fresh.detail).toContain("has not checked yet");

    const fired = describeMonitorSchedule(
      { ...monitor, last_dispatched_at: "2026-08-10T09:00:00Z" },
      true,
    );
    expect(fired.detail).toContain("Last checked");
  });
});

describe("formMetric", () => {
  it("reads a form-built query back into the form's operation and field", () => {
    expect(
      formMetric({ metrics: [buildMetric("sum", "amount")], limit: 1 }),
    ).toEqual({ operation: "sum", field: "amount" });
    expect(formMetric({ metrics: [buildMetric("count", "")], limit: 1 })).toEqual({
      operation: "count",
      field: "",
    });
  });

  it("refuses anything the form has no controls for — an edit must never silently drop it", () => {
    // Filters, grouping, ordering, a second metric: each one would be lost if
    // the form rebuilt the query from its own three controls.
    expect(
      formMetric({
        metrics: [buildMetric("sum", "amount")],
        filters: [{ field: "region", operator: "eq", value: "EU" }],
      }),
    ).toBeNull();
    expect(
      formMetric({ metrics: [buildMetric("sum", "amount")], group_by: "region" }),
    ).toBeNull();
    expect(
      formMetric({ metrics: [buildMetric("sum", "amount")], order_by: "amount" }),
    ).toBeNull();
    expect(
      formMetric({
        metrics: [buildMetric("sum", "amount"), buildMetric("count", "")],
      }),
    ).toBeNull();
    expect(formMetric({ metrics: [] })).toBeNull();
    expect(formMetric({})).toBeNull();
    // A hand-written label is part of the stored contract too: rebuilding
    // would rename the value every alert and dashboard sentence uses.
    expect(
      formMetric({
        metrics: [{ operation: "sum", field: "amount", label: "revenue" }],
      }),
    ).toBeNull();
  });
});

describe("monitorUpdatePayload", () => {
  it("sends ONLY the changed fields — an untouched definition field must not re-arm a tripped monitor", () => {
    // The server resets edge state whenever a definition field is among the
    // sent fields (by design); re-sending an unchanged threshold would turn
    // "rename this monitor" into "forget it already tripped".
    expect(
      monitorUpdatePayload(monitor, {
        name: "Renamed floor",
        comparator: monitor.comparator,
        threshold: monitor.threshold,
        schedule_cron: monitor.schedule_cron,
        schedule_timezone: monitor.schedule_timezone,
        dataset_id: monitor.dataset_id,
        query: null,
      }),
    ).toEqual({ name: "Renamed floor" });
  });

  it("returns an empty payload for an untouched form", () => {
    expect(
      monitorUpdatePayload(monitor, {
        name: monitor.name,
        comparator: monitor.comparator,
        threshold: monitor.threshold,
        schedule_cron: monitor.schedule_cron,
        schedule_timezone: monitor.schedule_timezone,
        dataset_id: monitor.dataset_id,
        query: null,
      }),
    ).toEqual({});
  });

  it("carries every genuinely changed field, and a rebuilt query only when given one", () => {
    const query = { metrics: [buildMetric("avg", "latency")], limit: 1 };
    expect(
      monitorUpdatePayload(monitor, {
        name: monitor.name,
        comparator: "gte",
        threshold: 250,
        schedule_cron: "0 * * * *",
        schedule_timezone: "UTC",
        dataset_id: "d2",
        query,
      }),
    ).toEqual({
      comparator: "gte",
      threshold: 250,
      schedule_cron: "0 * * * *",
      schedule_timezone: "UTC",
      dataset_id: "d2",
      query,
    });
  });

  it("treats a null dataset as untouched — the verbatim-query edit never re-sends it", () => {
    expect(
      monitorUpdatePayload(monitor, {
        name: monitor.name,
        comparator: monitor.comparator,
        threshold: 200,
        schedule_cron: monitor.schedule_cron,
        schedule_timezone: monitor.schedule_timezone,
        dataset_id: null,
        query: null,
      }),
    ).toEqual({ threshold: 200 });
  });
});
