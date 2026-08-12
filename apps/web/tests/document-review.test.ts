import { cleanup, render, screen } from "@testing-library/react";
import { createElement } from "react";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PendingDocumentEdit } from "@workspace/api-client";
import { DocumentReview } from "../components/views/document-review";

/**
 * The inline reviewer's one job that no type can enforce: several hunk choices
 * must leave as *one* decision.
 *
 * The server turns a single `accepted_hunks` approval into one version row
 * whose summary counts what was applied ("1 of 2 proposed changes applied").
 * A reviewer deciding hunk by hunk over the wire would instead write a version
 * per click — a history of partial states nobody ever read — and, since a tool
 * call can only be decided once, every click after the first would 409.
 */
const EDIT: PendingDocumentEdit = {
  id: "call-1",
  run_id: "run-1",
  name: "edit_document",
  document_id: "doc-1",
  title: "Launch Runbook",
  proposal_preview: "@@\n-Alpha needs review.\n+Alpha was reviewed.",
  segments: [
    { index: 0, before: ["Alpha needs review."], after: ["Alpha was reviewed."] },
    { index: -1, before: ["Beta stays."], after: ["Beta stays."] },
    { index: 1, before: ["Gamma needs review."], after: ["Gamma was reviewed."] },
  ],
  created_at: "2026-01-01T00:00:00Z",
};

const decide = vi.fn();

function click(name: RegExp | string) {
  const button = screen.getByRole("button", { name });
  act(() => {
    button.click();
  });
}

beforeEach(() => {
  decide.mockReset();
  decide.mockResolvedValue(undefined);
  render(createElement(DocumentReview, { edit: EDIT, decide }));
});

afterEach(cleanup);

describe("DocumentReview", () => {
  it("starts by taking the whole proposal, which is what Approve used to mean", () => {
    // Nothing is silently decided for a reviewer who reads and presses Apply:
    // the default is the pre-existing behaviour, and the count says so.
    expect(screen.getByRole("button", { name: /^Apply 2 of 2 changes/ })).toBeTruthy();
  });

  it("stages every choice into one decision naming the accepted hunks", () => {
    click(/^Reject change 2/);
    // The button is the running total, so the user is never guessing.
    expect(screen.getByRole("button", { name: /^Apply 1 of 2 changes/ })).toBeTruthy();
    click(/^Apply 1 of 2 changes/);

    expect(decide).toHaveBeenCalledTimes(1);
    expect(decide).toHaveBeenCalledWith(EDIT, "approved", [0]);
  });

  it("sends the zero-based wire index, not the number on the label", () => {
    // "Change 1" is for the reader; hunk 0 is what the server accepts by. An
    // off-by-one here applies a change the reviewer looked at and declined.
    click(/^Reject change 1/);
    click(/^Apply 1 of 2 changes/);
    expect(decide).toHaveBeenCalledWith(EDIT, "approved", [1]);
  });

  it("survives a hunk being rejected and taken back", () => {
    click(/^Reject change 1/);
    click(/^Accept change 1/);
    click(/^Apply 2 of 2 changes/);
    expect(decide).toHaveBeenCalledWith(EDIT, "approved", [0, 1]);
  });

  it("turns rejecting everything into a denial, never an empty approval", () => {
    // The server refuses an empty `accepted_hunks`, and rightly: "approved,
    // and apply none of it" is not a thing a reviewer can have meant. Sending
    // it anyway would surface as a 409 on a button that read "Apply".
    click(/^Reject change 1/);
    click(/^Reject change 2/);
    click(/^Reject all changes/);
    expect(decide).toHaveBeenCalledTimes(1);
    expect(decide).toHaveBeenCalledWith(EDIT, "denied");
  });

  it("marks the chosen side of each hunk in the control, not only in colour", () => {
    // A struck-through line is not something a low-vision reader should have
    // to detect to know which way a hunk is going.
    const accept = screen.getByRole("button", { name: /^Accept change 1/ });
    const reject = screen.getByRole("button", { name: /^Reject change 1/ });
    expect(accept.getAttribute("aria-pressed")).toBe("true");
    expect(reject.getAttribute("aria-pressed")).toBe("false");
    click(/^Reject change 1/);
    expect(accept.getAttribute("aria-pressed")).toBe("false");
    expect(reject.getAttribute("aria-pressed")).toBe("true");
  });
});
