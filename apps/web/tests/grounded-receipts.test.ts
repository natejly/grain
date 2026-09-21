// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { GroundedReceipt } from "@workspace/api-client";

const listGroundedReceipts = vi.fn();
const getGroundedReceipt = vi.fn();

vi.mock("../components/api", () => ({
  api: {
    listGroundedReceipts: (...a: unknown[]) => listGroundedReceipts(...a),
    getGroundedReceipt: (...a: unknown[]) => getGroundedReceipt(...a),
  },
}));

import { GroundedReceiptsPanel } from "../components/views/grounded-receipts";

/**
 * The ledger row, and the one number it must not invent.
 *
 * `GroundingCheck` states the rule in the schema itself: "`scored == 0` means
 * nothing was gradable here — which is not 'ungrounded', and must not be
 * rendered as a 0%". `describeGrounding` obeys it by rendering nothing, and
 * `grounded.verdict_line` by saying "no sentence in this answer made a
 * checkable claim". This row printed `0% grounded` regardless — so the summary
 * asserted a failure about an answer that made no checkable claim, while the
 * drawer one click below, reading the same verdict, showed no plate at all.
 */

function receipt(overrides: Partial<GroundedReceipt> = {}): GroundedReceipt {
  return {
    id: "receipt-1",
    question: "What is the retention window?",
    evidence_count: 3,
    grounding_score: 0.5,
    scored: 2,
    valid: true,
    embedding_generation_id: "",
    created_at: new Date().toISOString(),
    ...overrides,
  };
}

afterEach(cleanup);

beforeEach(() => {
  listGroundedReceipts.mockReset();
  getGroundedReceipt.mockReset();
});

describe("GroundedReceiptsPanel", () => {
  it("prints the percentage when something was actually graded", async () => {
    listGroundedReceipts.mockResolvedValue([receipt()]);
    render(React.createElement(GroundedReceiptsPanel, { setError: () => {} }));
    await waitFor(() => expect(screen.getByText(/50% grounded/)).toBeTruthy());
  });

  it("says 'no checkable claim' instead of 0% when nothing was gradable", async () => {
    listGroundedReceipts.mockResolvedValue([
      receipt({ id: "receipt-2", grounding_score: 0, scored: 0 }),
    ]);
    render(React.createElement(GroundedReceiptsPanel, { setError: () => {} }));

    await waitFor(() => expect(screen.getByText(/no checkable claim/)).toBeTruthy());
    expect(screen.queryByText(/0% grounded/)).toBeNull();
    // The wording matches `grounded.verdict_line` and the drawer's silence, so
    // the row and the detail below it tell one story.
    expect(screen.queryByText(/ungrounded/i)).toBeNull();
  });
});
