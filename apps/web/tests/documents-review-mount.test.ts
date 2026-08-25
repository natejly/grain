import { cleanup, render, screen } from "@testing-library/react";
import { act, createElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { PendingDocumentEdit, WorkspaceDocument } from "@workspace/api-client";
import { DocumentsView } from "../components/views/documents";

/**
 * Where the inline reviewer is *mounted*, which is a different claim from what
 * it does once it is.
 *
 * `DocumentReview` holds the reviewer's staged rejections in its own state, and
 * that state is only ever correct for the proposal it was staged against. A
 * second proposal can replace the first without the component unmounting — the
 * first was decided somewhere else (the Activity queue, the rail's chat, a
 * second tab) and the next `refreshPendingEdits` brings a new one for the same
 * document. React reuses an instance whose position and type have not changed,
 * so without a key the new diff opens with hunks already crossed out that
 * nobody crossed out, and the Apply count agrees with them. That is the worst
 * shape this feature can fail in: it applies a subset the reviewer never chose
 * while telling them it is theirs.
 */
const DOCUMENT: WorkspaceDocument = {
  id: "doc-1",
  title: "Launch Runbook",
  kind: "markdown",
  content: "Alpha needs review.\nBeta stays.\nGamma needs review.\n",
  folder_id: "",
  updated_at: "2026-01-01T00:00:00Z",
};

function edit(id: string): PendingDocumentEdit {
  return {
    id,
    run_id: "run-1",
    name: "edit_document",
    document_id: DOCUMENT.id,
    title: DOCUMENT.title,
    proposal_preview: "@@",
    segments: [
      { index: 0, before: ["Alpha needs review."], after: ["Alpha was reviewed."] },
      { index: -1, before: ["Beta stays."], after: ["Beta stays."] },
      { index: 1, before: ["Gamma needs review."], after: ["Gamma was reviewed."] },
    ],
    created_at: "2026-01-01T00:00:00Z",
  };
}

const noop = async () => undefined;

function view(pending: PendingDocumentEdit[]) {
  return createElement(DocumentsView, {
    documents: [
      {
        id: DOCUMENT.id,
        title: DOCUMENT.title,
        kind: DOCUMENT.kind,
        characters: DOCUMENT.content.length,
        folder_id: "",
        updated_at: DOCUMENT.updated_at,
      },
    ],
    folders: [],
    folderOps: {
      createFolder: noop,
      renameFolder: noop,
      moveFolder: noop,
      removeFolder: noop,
      moveDocument: noop,
    },
    active: DOCUMENT,
    versions: [],
    openDocument: noop,
    createDocument: noop,
    saveDocument: noop,
    restoreVersion: noop,
    removeDocument: noop,
    pendingEdits: pending,
    decidePendingEdit: vi.fn().mockResolvedValue(undefined),
    // No `chat`: the panel is a separate surface with its own network, and the
    // claim under test is about the editor column.
  });
}

afterEach(cleanup);

describe("the inline reviewer inside the document editor", () => {
  it("is the editor's replacement while a hunked proposal is open", () => {
    render(view([edit("call-1")]));
    // The textarea is gone, not merely covered: a user typing into text the
    // agent is asking to change would race the decision, and whichever wrote
    // last would silently win.
    expect(screen.queryByLabelText("Document source")).toBeNull();
    expect(screen.getByRole("region", { name: "Proposed changes" })).toBeTruthy();
  });

  it("does not carry one proposal's staged rejections onto the next", () => {
    const { rerender } = render(view([edit("call-1")]));
    act(() => {
      screen.getByRole("button", { name: /^Reject change 2/ }).click();
    });
    expect(screen.getByRole("button", { name: /^Apply 1 of 2 changes/ })).toBeTruthy();

    // The first proposal was decided elsewhere and a second arrived for the
    // same document. Same position, same component type — only the id differs.
    rerender(view([edit("call-2")]));

    // Back to "take it whole", which is what Approve has always meant. The
    // count is the assertion: it is what the reviewer reads before pressing,
    // and a stale rejection would have it promise a subset nobody chose.
    expect(screen.getByRole("button", { name: /^Apply 2 of 2 changes/ })).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /^Accept change 2/ }).getAttribute("aria-pressed"),
    ).toBe("true");
  });
});

/**
 * "Later": review is the DEFAULT for a newly-arrived proposal — the e2e specs
 * pin that the region appears without a click — but it is no longer a cell the
 * user cannot leave. The exit parks the proposal behind a banner over the
 * editor, and the banner has to make the interlock honest: the textarea comes
 * back read-only, because the swap always prevented a concurrent edit and the
 * parked state must not silently allow a race the server would reject.
 */
describe("parking the reviewer with Later", () => {
  it("returns a read-only editor under a banner that counts the changes", () => {
    render(view([edit("call-1")]));
    act(() => {
      screen.getByRole("button", { name: "Later" }).click();
    });

    // The reviewer is parked, not decided: no region, editor back on screen.
    expect(screen.queryByRole("region", { name: "Proposed changes" })).toBeNull();
    const source = screen.getByLabelText("Document source") as HTMLTextAreaElement;
    expect(source.readOnly).toBe(true);
    // Two decidable hunks (the third segment is context), counted honestly.
    expect(screen.getByText("The agent proposed 2 changes")).toBeTruthy();
    expect(screen.getByText(/Editing is paused while changes are pending/)).toBeTruthy();
  });

  it("reopens through the banner's Review button", () => {
    render(view([edit("call-1")]));
    act(() => {
      screen.getByRole("button", { name: "Later" }).click();
    });
    act(() => {
      screen.getByRole("button", { name: "Review" }).click();
    });
    expect(screen.getByRole("region", { name: "Proposed changes" })).toBeTruthy();
    expect(screen.queryByLabelText("Document source")).toBeNull();
  });

  it("mounts review afresh when a new proposal id arrives while parked", () => {
    const { rerender } = render(view([edit("call-1")]));
    act(() => {
      screen.getByRole("button", { name: "Later" }).click();
    });
    expect(screen.queryByRole("region", { name: "Proposed changes" })).toBeNull();

    // The first proposal was decided elsewhere; a second arrives. "Later"
    // answered the old proposal's nag, not the new one's.
    rerender(view([edit("call-2")]));
    expect(screen.getByRole("region", { name: "Proposed changes" })).toBeTruthy();
    // And fresh: every hunk starts accepted, nothing carried from call-1.
    expect(screen.getByRole("button", { name: /^Apply 2 of 2 changes/ })).toBeTruthy();
  });
});
