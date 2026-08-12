import type { Cron } from "@workspace/api-client";
import { describe, expect, it } from "vitest";
import { describeCronSchedule } from "../components/views/crons";

/**
 * `describeCronSchedule` is the one thing the Automations view is careful never
 * to overstate: a stored cron expression is an intention, and only an armed
 * ticker makes it a promise. These pin the same ADR-0007 discipline the
 * Workflows view's `describeSchedule` carries — a branch that cannot promise a
 * dispatch says so in the *headline*, not the small print — plus the one rule
 * this sibling adds: `enabled` is believed before any armed-state, because an
 * operator who switched a cron off should never read "Runs on …".
 */
describe("describeCronSchedule", () => {
  const cron: Cron = {
    id: "c1",
    name: "Morning digest",
    kind: "task",
    schedule_cron: "0 9 * * 1",
    schedule_timezone: "Europe/London",
    enabled: true,
    prompt: "Summarise yesterday's open pull requests.",
    body: "",
    target_conversation_id: "",
    last_dispatched_at: null,
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:00Z",
  };

  it("says a disabled cron is fired by nobody, whatever the ticker is doing", () => {
    // enabled is checked first: a live ticker must not talk an operator out of
    // the off switch they just flipped.
    const note = describeCronSchedule({ ...cron, enabled: false }, true);
    expect(note.tone).toBe("warn");
    expect(note.headline).toBe("Disabled — nothing will fire it");
    // Still quotes the schedule so they can see what enabling would run.
    expect(note.detail).toContain("0 9 * * 1 · Europe/London");
  });

  it("keeps disabled ahead of an unarmed ticker, not doubly-warned about the wrong thing", () => {
    const note = describeCronSchedule({ ...cron, enabled: false }, false);
    expect(note.headline).toBe("Disabled — nothing will fire it");
  });

  it("refuses to call an enabled cron scheduled when nothing can fire it", () => {
    const note = describeCronSchedule(cron, false);
    expect(note.tone).toBe("warn");
    expect(note.headline).toContain("nothing will fire it");
    expect(note.detail).toContain("Run now");
  });

  it("treats an unanswered ticker as unknown, not as yes", () => {
    const note = describeCronSchedule(cron, null);
    expect(note.tone).toBe("warn");
    expect(note.headline).toContain("did not answer");
    expect(note.detail).toContain("treat this as unscheduled");
  });

  it("says it runs, and quotes the cron verbatim, only when it really does", () => {
    const note = describeCronSchedule(cron, true);
    expect(note.tone).toBe("live");
    // Verbatim, never reworded into "every Monday at 9am".
    expect(note.headline).toBe("Runs on 0 9 * * 1 · Europe/London");
    expect(note.detail).toContain("not fired yet");
  });

  it("reports the last fire once the ticker has dispatched it", () => {
    const note = describeCronSchedule(
      { ...cron, last_dispatched_at: "2026-08-10T09:00:00Z" },
      true,
    );
    expect(note.tone).toBe("live");
    expect(note.detail).toContain("Last fired");
    expect(note.detail).not.toContain("not fired yet");
  });
});
