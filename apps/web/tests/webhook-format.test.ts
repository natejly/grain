import { describe, expect, it } from "vitest";
import {
  WEBHOOK_EVENTS,
  deliveriesFor,
  deliveryLabel,
  deliveryTone,
  eventLabel,
  tokenState,
  tokenUseLabel,
} from "../components/views/webhook-format";
import type { WebhookDelivery } from "@workspace/api-client";

function delivery(overrides: Partial<WebhookDelivery> = {}): WebhookDelivery {
  return {
    id: "d1",
    endpoint_id: "e1",
    event: "run.completed",
    status: "pending",
    attempts: 0,
    last_error: "",
    created_at: "2026-08-25T09:00:00",
    sent_at: null,
    ...overrides,
  };
}

describe("the event vocabulary", () => {
  it("offers exactly the four events the server emits, labelled", () => {
    // Pinned as an array: the form renders these in order, and a fifth event
    // added server-side must be added here on purpose, with a label.
    expect(WEBHOOK_EVENTS.map((entry) => entry.event)).toEqual([
      "run.completed",
      "workflow_run.completed",
      "approval.requested",
      "monitor.tripped",
    ]);
  });

  it("labels known events and passes strangers through verbatim", () => {
    expect(eventLabel("monitor.tripped")).toBe("Monitor tripped");
    // An event this build has never heard of (an older client against a newer
    // server) must still render something honest, not blank.
    expect(eventLabel("digest.sent")).toBe("digest.sent");
  });
});

describe("token rows", () => {
  it("reads state off the revocation stamp", () => {
    expect(tokenState({ revoked_at: null })).toBe("active");
    expect(tokenState({ revoked_at: "2026-08-25T00:00:00" })).toBe("revoked");
  });

  it("says out loud when a credential has never been used", () => {
    expect(tokenUseLabel({ last_used_at: null })).toBe("Never used");
    expect(tokenUseLabel({ last_used_at: "2026-08-01T00:00:00" })).toMatch(
      /^Last used /,
    );
  });
});

describe("delivery chips", () => {
  it("maps status onto the shell's pill tones", () => {
    expect(deliveryTone(delivery({ status: "sent" }))).toBe("ready");
    expect(deliveryTone(delivery({ status: "failed" }))).toBe("error");
    // Pending is a queue, not a problem — neutral, never red.
    expect(deliveryTone(delivery({ status: "pending" }))).toBe("");
  });

  it("distinguishes waiting from retrying, and a failure names its evidence", () => {
    expect(deliveryLabel(delivery())).toBe("Pending");
    expect(deliveryLabel(delivery({ attempts: 2 }))).toBe("Retrying");
    expect(deliveryLabel(delivery({ status: "sent" }))).toBe("Delivered");
    expect(
      deliveryLabel(
        delivery({
          status: "failed",
          attempts: 3,
          last_error: "endpoint answered 500",
        }),
      ),
    ).toBe("Failed after 3 attempts: endpoint answered 500");
    expect(
      deliveryLabel(delivery({ status: "failed", attempts: 1 })),
    ).toBe("Failed after 1 attempt");
  });
});

describe("deliveriesFor", () => {
  const rows = [
    delivery({ id: "old", created_at: "2026-08-24T09:00:00" }),
    delivery({ id: "new", created_at: "2026-08-25T09:00:00" }),
    delivery({ id: "other", endpoint_id: "e2", created_at: "2026-08-26T09:00:00" }),
  ];

  it("orders newest first and filters to one endpoint when asked", () => {
    expect(deliveriesFor(rows).map((row) => row.id)).toEqual([
      "other",
      "new",
      "old",
    ]);
    expect(deliveriesFor(rows, "e1").map((row) => row.id)).toEqual([
      "new",
      "old",
    ]);
  });
});
