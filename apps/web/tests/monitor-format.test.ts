import type { Monitor } from "@workspace/api-client";
import { describe, expect, it } from "vitest";
import {
  describeMonitorSchedule,
  lastValueCopy,
  metricLabel,
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
