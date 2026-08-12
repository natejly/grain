import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The Documents view's approval card disables both buttons while a decision is
 * in flight and re-enables them only in its own `catch`, on the stated
 * assumption that a success removes the card outright. That makes the handler's
 * rejection the card's only route back to an enabled state: if
 * `decidePendingEdit` swallows a failed decision, the card sits at "Applying…"
 * with Approve and Deny disabled forever and the edit still listed, and only a
 * page reload gets the user out.
 */
const decideAgentToolCall = vi.fn();
const getDocument = vi.fn();
const listDocumentVersions = vi.fn();
const streamRun = vi.fn();

vi.mock("../components/api", () => ({
  api: {
    decideAgentToolCall: (...args: unknown[]) => decideAgentToolCall(...args),
    getDocument: (...args: unknown[]) => getDocument(...args),
    listDocumentVersions: (...args: unknown[]) => listDocumentVersions(...args),
    streamRun: (...args: unknown[]) => streamRun(...args),
  },
}));

import { createDocumentHandlers } from "../components/handlers/documents";

const EDIT = {
  id: "call-1",
  run_id: "run-1",
  name: "edit_document",
  document_id: "doc-1",
  title: "Launch Runbook",
  proposal_preview: "@@\n-a\n+b",
  created_at: "2026-01-01T00:00:00Z",
};

function handlers() {
  const state = {
    error: "",
    pending: [EDIT],
  };
  const noop = () => undefined;
  const made = createDocumentHandlers({
    setError: ((value: string) => {
      state.error = value;
    }) as never,
    setDocuments: noop as never,
    setActiveDocument: noop as never,
    setDocumentVersions: noop as never,
    setPendingEdits: ((update: (items: typeof state.pending) => typeof state.pending) => {
      state.pending = update(state.pending);
    }) as never,
    refreshArtifacts: async () => undefined,
    activeDocumentRef: { current: "doc-1" },
  });
  return { made, state };
}

/** An event stream that only reports this call's completion when told to. */
function gatedStream(events: { event: string; data: Record<string, unknown> }[]) {
  let release = () => undefined as void;
  const gate = new Promise<void>((resolve) => {
    release = () => resolve();
  });
  streamRun.mockImplementation(async function* () {
    for (const event of events) {
      if (event.event === "tool.completed") await gate;
      yield { id: 0, ...event };
    }
  });
  return () => release();
}

describe("decidePendingEdit", () => {
  beforeEach(() => {
    decideAgentToolCall.mockReset();
    getDocument.mockReset();
    listDocumentVersions.mockReset();
    getDocument.mockResolvedValue({ id: "doc-1", content: "after" });
    listDocumentVersions.mockResolvedValue([]);
    streamRun.mockReset();
    streamRun.mockImplementation(async function* () {
      yield { id: 1, event: "tool.completed", data: { tool_call_id: EDIT.id } };
    });
  });

  it("drops the edit from the list once the decision is recorded", async () => {
    decideAgentToolCall.mockResolvedValue({ id: "call-1", status: "approved" });
    const { made, state } = handlers();
    await made.decidePendingEdit(EDIT as never, "approved");
    // No amendment: an approval with no hunk selection has to reach the server
    // as the whole proposal, not as an empty `accepted_hunks` — which the
    // server refuses outright, and rightly.
    expect(decideAgentToolCall).toHaveBeenCalledWith("call-1", "approved", false, {});
    expect(state.pending).toEqual([]);
  });

  /**
   * The inline reviewer stages every hunk choice and sends them together. One
   * decision is the whole point: the server turns a single `accepted_hunks`
   * approval into one version row whose summary counts what was applied, so a
   * per-click decision would write a version per click and leave a history of
   * partial states nobody ever read — and only the first click could succeed
   * anyway, since deciding a call twice is a 409.
   */
  it("sends a partial approval as one decision carrying the accepted hunks", async () => {
    decideAgentToolCall.mockResolvedValue({ id: "call-1", status: "approved" });
    const { made, state } = handlers();
    await made.decidePendingEdit(EDIT as never, "approved", [0, 2]);
    expect(decideAgentToolCall).toHaveBeenCalledTimes(1);
    expect(decideAgentToolCall).toHaveBeenCalledWith("call-1", "approved", false, {
      accepted_hunks: [0, 2],
    });
    expect(state.pending).toEqual([]);
  });

  it("rejects when the decision fails, so the card can re-enable its buttons", async () => {
    decideAgentToolCall.mockRejectedValue(new Error("Tool call already decided"));
    const { made, state } = handlers();
    await expect(made.decidePendingEdit(EDIT as never, "approved")).rejects.toThrow(
      "Tool call already decided",
    );
    // A failed decision leaves the proposal outstanding; hiding it would tell
    // the user a write was handled when it was not.
    expect(state.pending).toEqual([EDIT]);
  });

  it("refetches the document only after the approved call has actually run", async () => {
    decideAgentToolCall.mockResolvedValue({ id: "call-1", status: "approved" });
    getDocument.mockResolvedValue({ id: "doc-1", content: "after" });
    listDocumentVersions.mockResolvedValue([]);
    // The write lands in a background task, so a refetch issued before the tool
    // reports completion reads the pre-write document and strands the editor.
    const runTheTool = gatedStream([
      { event: "run.started", data: {} },
      { event: "tool.completed", data: { tool_call_id: EDIT.id } },
    ]);

    const { made } = handlers();
    const settled = made.decidePendingEdit(EDIT as never, "approved");
    // Drain every continuation already queued. A handler that refetches without
    // waiting on the tool has issued its read by this point.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(getDocument).not.toHaveBeenCalled();

    runTheTool();
    await settled;
    expect(getDocument).toHaveBeenCalledWith("doc-1");
  });

  it("stops following a run that ends without reaching the call", async () => {
    decideAgentToolCall.mockResolvedValue({ id: "call-1", status: "denied" });
    getDocument.mockResolvedValue({ id: "doc-1", content: "unchanged" });
    listDocumentVersions.mockResolvedValue([]);
    // A cancelled run never emits tool.completed; waiting on it would hang the
    // handler forever and leave the view permanently unrefreshed.
    streamRun.mockImplementation(async function* () {
      yield { id: 1, event: "run.cancelled", data: {} };
      throw new Error("kept reading past the end of the run");
    });

    const { made } = handlers();
    await made.decidePendingEdit(EDIT as never, "denied");
    expect(getDocument).toHaveBeenCalledWith("doc-1");
  });
});
