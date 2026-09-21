import type { DeliverableManifest } from "@workspace/api-client";
import { describe, expect, it } from "vitest";
import {
  describeManifestStatus,
  describeSpend,
} from "../components/views/deliverable-format";

/**
 * A manifest is the receipt for the only per-run cost control this product
 * has, so these assertions are about not claiming the control worked.
 *
 * `partial` is written by EVERY terminal halt — a budget running out, a
 * cancellation, a failed node — so the copy cannot read it as "budget
 * exhausted". The budgets and the spend are both on the manifest, so the
 * reason is derived from the numbers rather than guessed from the status.
 */

function manifest(overrides: Partial<DeliverableManifest> = {}): DeliverableManifest {
  return {
    id: "manifest-1",
    workflow_run_id: "wr-1",
    space_id: "",
    title: "Retention review",
    status: "complete",
    budget_seconds: 900,
    budget_tool_calls: 40,
    spent_seconds: 120,
    spent_tool_calls: 12,
    created_by: "user-1",
    created_at: "2026-09-21T09:00:00",
    ...overrides,
  };
}

describe("describeManifestStatus", () => {
  it("does not call a cancellation a budget exhaustion", () => {
    expect(
      describeManifestStatus(manifest({ status: "partial", spent_tool_calls: 3 })),
    ).toBe("stopped early — partial results");
  });

  it("names the budget that actually ran out", () => {
    expect(
      describeManifestStatus(
        manifest({ status: "partial", spent_tool_calls: 40 }),
      ),
    ).toBe("tool-call budget exhausted — partial results");
    expect(
      describeManifestStatus(
        manifest({
          status: "partial",
          budget_tool_calls: 0,
          spent_seconds: 900,
        }),
      ),
    ).toBe("time budget exhausted — partial results");
  });

  it("says nothing about budgets for a completed run", () => {
    expect(describeManifestStatus(manifest())).toBe("complete");
  });
});

describe("describeSpend", () => {
  it("shows both halves, and renders no limit as a dash", () => {
    expect(describeSpend(manifest())).toBe("12 of 40 tool calls · 120s of 900s");
    expect(
      describeSpend(manifest({ budget_seconds: 0, budget_tool_calls: 0 })),
    ).toBe("12 of — tool calls · 120s of —");
  });
});
