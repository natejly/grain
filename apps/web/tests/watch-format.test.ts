import type { Watch } from "@workspace/api-client";
import { describe, expect, it } from "vitest";
import {
  MAX_EXTRACTION_FIELDS,
  SHARED_HELPER_TEXT,
  describeLastChange,
  describeRunNow,
  describeSchedule,
  describeTarget,
} from "../components/views/watch-format";

/**
 * One of these assertions is not like the others. `shared` is the only control
 * in this family that widens memory visibility — it decides whether what the
 * watch learns lands on everyone's shelf or only its creator's — so its copy
 * is pinned here in the words the server means, not in words that merely sound
 * like sharing.
 */

function watch(overrides: Partial<Watch> = {}): Watch {
  return {
    id: "watch-1",
    name: "Release notes",
    target_kind: "source",
    target_id: "source-1",
    space_id: "",
    shared: false,
    schedule_cron: "0 9 * * *",
    schedule_timezone: "UTC",
    enabled: true,
    extraction_fields: ["owner"],
    brief_document_id: "",
    last_checked_at: null,
    last_change_at: null,
    created_by: "user-1",
    created_at: "2026-09-21T09:00:00",
    ...overrides,
  };
}

const relative = (iso: string) => `at ${iso.slice(0, 10)}`;

describe("target and schedule labels", () => {
  it("names the kind and the thing", () => {
    expect(describeTarget(watch(), "report.pdf")).toBe("source: report.pdf");
    expect(
      describeTarget(watch({ target_kind: "space" }), "Research"),
    ).toBe("space: Research");
  });

  it("falls back to the id when the target's name is not loaded", () => {
    expect(describeTarget(watch(), "   ")).toBe("source: source-1");
  });

  it("shows the expression and the zone it fires in", () => {
    expect(describeSchedule(watch())).toBe("0 9 * * * · UTC");
  });
});

describe("the three last-change states", () => {
  it("keeps 'never run' apart from 'ran and nothing changed'", () => {
    // "Checked, no change yet" is a working watch; "never run" is a
    // configuration problem. Conflating them would hide a broken schedule.
    expect(describeLastChange(watch(), relative)).toBe("Never run");
    expect(
      describeLastChange(
        watch({ last_checked_at: "2026-09-21T09:00:00" }),
        relative,
      ),
    ).toBe("Checked at 2026-09-21 — no change yet");
  });

  it("reports the last change once there has been one", () => {
    expect(
      describeLastChange(
        watch({
          last_checked_at: "2026-09-21T09:00:00",
          last_change_at: "2026-09-20T09:00:00",
        }),
        relative,
      ),
    ).toBe("Last change at 2026-09-20");
  });
});

describe("the shared checkbox copy", () => {
  it("says what the server means: whose memory shelf this writes to", () => {
    expect(SHARED_HELPER_TEXT).toBe(
      "A shared watch writes what it learns to everyone’s memory shelf; otherwise it writes to yours.",
    );
    expect(SHARED_HELPER_TEXT).toContain("memory shelf");
  });

  it("caps declared fields at the server's own limit", () => {
    // The memory row count is bounded by declared fields, not by fires, so
    // this cap is the whole of the supersession-churn budget.
    expect(MAX_EXTRACTION_FIELDS).toBe(10);
  });
});

describe("run-now feedback", () => {
  it("reports 'nothing changed' plainly rather than silently", () => {
    expect(describeRunNow(false)).toBe(
      "Checked — nothing has changed, so nothing was written.",
    );
    expect(describeRunNow(true)).toContain("the brief was rewritten");
  });
});
