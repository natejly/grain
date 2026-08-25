import { describe, expect, it } from "vitest";
import type { DashboardSubscription } from "@workspace/api-client";
import {
  SUBSCRIPTION_PRESETS,
  describeSubscriptionSchedule,
  subscriptionsFor,
} from "../components/views/subscription-format";

function subscription(
  overrides: Partial<DashboardSubscription> = {},
): DashboardSubscription {
  return {
    id: "sub-1",
    dashboard_id: "dash-1",
    dashboard_name: "Revenue",
    recipient_user_id: "user-1",
    schedule_cron: "0 9 * * *",
    schedule_timezone: "UTC",
    enabled: true,
    last_dispatched_at: null,
    created_by: "user-1",
    created_at: "2026-08-25T10:00:00",
    ...overrides,
  };
}

describe("SUBSCRIPTION_PRESETS", () => {
  it("offers exactly the two shapes a snapshot mail is asked for, as real cron strings", () => {
    // Pinned exactly: the popover builds server payloads from these strings,
    // and a preset silently drifting to a different minute would change when
    // everyone's mail arrives without anyone touching a subscription.
    expect(SUBSCRIPTION_PRESETS.map((preset) => preset.cron)).toEqual([
      "0 9 * * *",
      "0 9 * * 1",
    ]);
    expect(SUBSCRIPTION_PRESETS.map((preset) => preset.label)).toEqual([
      "Every day at 9:00",
      "Mondays at 9:00",
    ]);
  });
});

describe("describeSubscriptionSchedule", () => {
  it("speaks a preset's words when the stored cron matches one", () => {
    expect(describeSubscriptionSchedule("0 9 * * *", "Europe/Berlin")).toBe(
      "Every day at 9:00 · Europe/Berlin",
    );
    expect(describeSubscriptionSchedule("0 9 * * 1", "UTC")).toBe(
      "Mondays at 9:00 · UTC",
    );
  });

  it("shows an unrecognised cron honestly instead of guessing at words", () => {
    // A subscription created through the API can carry any 5-field cron; the
    // popover must not translate one it does not know into the wrong sentence.
    expect(describeSubscriptionSchedule("30 6 * * 5", "UTC")).toBe(
      "30 6 * * 5 · UTC",
    );
  });

  it("always names a zone — 9:00 without a timezone is a number pretending to be a time", () => {
    expect(describeSubscriptionSchedule("0 9 * * *", "")).toBe(
      "Every day at 9:00 · UTC",
    );
  });
});

describe("subscriptionsFor", () => {
  it("keeps only the subscriptions about this one dashboard, newest first", () => {
    const rows = [
      subscription({ id: "a", created_at: "2026-08-25T09:00:00" }),
      subscription({ id: "other", dashboard_id: "dash-2" }),
      subscription({ id: "b", created_at: "2026-08-25T10:00:00" }),
    ];
    expect(subscriptionsFor(rows, "dash-1").map((row) => row.id)).toEqual([
      "b",
      "a",
    ]);
  });
});
