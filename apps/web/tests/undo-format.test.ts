import { describe, expect, it } from "vitest";
import { UNDO_CONFIRM, summarizeUndo } from "../components/views/undo-format";

const reverted = (tool: string) => ({ tool_name: tool, kind: "document" });
const external = (tool: string) => ({
  tool_name: tool,
  reason: "external effects cannot be undone",
  outcome: "external",
});
const protectedSkip = (tool: string, what = "board") => ({
  tool_name: tool,
  reason: `the ${what} changed after this run; skipped to protect the later edits`,
  outcome: "protected",
});
const failedSkip = (tool: string) => ({
  tool_name: tool,
  reason: "restore failed: the dashboard no longer exists",
  outcome: "failed",
});

describe("summarizeUndo", () => {
  it("stays silent when everything reverted cleanly", () => {
    expect(
      summarizeUndo({
        run_id: "r1",
        reverted: [reverted("edit_document")],
        skipped: [],
      }),
    ).toEqual({ text: "", failed: false, retryable: false });
  });

  it("stays silent for a run that recorded nothing to undo", () => {
    expect(summarizeUndo({ run_id: "r1", reverted: [], skipped: [] })).toEqual({
      text: "",
      failed: false,
      retryable: false,
    });
  });

  it("carries the reason, not just the tool name", () => {
    // The whole point of the skip-and-report undo: "the board changed after
    // this run" is the sentence the user needs. A bare "board_add_card" is
    // indistinguishable from a crash.
    const outcome = summarizeUndo({
      run_id: "r1",
      reverted: [reverted("edit_document")],
      skipped: [protectedSkip("board_add_card")],
    });
    expect(outcome.text).toContain(
      "the board changed after this run; skipped to protect the later edits",
    );
    expect(outcome.text).toContain("board_add_card");
  });

  it("reports a protective skip as an outcome, never as a failure", () => {
    const outcome = summarizeUndo({
      run_id: "r1",
      reverted: [reverted("edit_document")],
      skipped: [protectedSkip("board_add_card")],
    });
    expect(outcome.failed).toBe(false);
    expect(outcome.retryable).toBe(true);
    expect(outcome.text).toContain("Undo the run again once those edits are settled.");
  });

  it("does not invite a retry that cannot help", () => {
    // An external effect is not going to become undoable by waiting.
    const outcome = summarizeUndo({
      run_id: "r1",
      reverted: [],
      skipped: [external("run_python")],
    });
    expect(outcome.retryable).toBe(false);
    expect(outcome.failed).toBe(false);
    expect(outcome.text).toBe(
      "Nothing was undone; 1 change not reverted — " +
        "run_python: external effects cannot be undone.",
    );
  });

  it("flags a restore that actually raised", () => {
    const outcome = summarizeUndo({
      run_id: "r1",
      reverted: [],
      skipped: [failedSkip("update_dashboard")],
    });
    expect(outcome.failed).toBe(true);
    expect(outcome.text).toContain("restore failed:");
  });

  it("is a failure when any one skip failed, even beside safe ones", () => {
    const outcome = summarizeUndo({
      run_id: "r1",
      reverted: [reverted("edit_document")],
      skipped: [external("run_python"), failedSkip("update_dashboard")],
    });
    expect(outcome.failed).toBe(true);
    expect(outcome.text).toContain("2 changes not reverted");
  });

  it("reports a row a concurrent undo consumed without alarming anyone", () => {
    const outcome = summarizeUndo({
      run_id: "r1",
      reverted: [],
      skipped: [
        {
          tool_name: "edit_document",
          reason: "already consumed by a concurrent undo",
          outcome: "concurrent",
        },
      ],
    });
    expect(outcome.failed).toBe(false);
    expect(outcome.retryable).toBe(false);
  });

  it("uses the singular for a single reverted change", () => {
    const outcome = summarizeUndo({
      run_id: "r1",
      reverted: [reverted("edit_document")],
      skipped: [external("run_command")],
    });
    expect(outcome.text).toBe(
      "Undid 1 change; 1 change not reverted — " +
        "run_command: external effects cannot be undone.",
    );
  });

  it("warns about external effects in the confirm copy", () => {
    expect(UNDO_CONFIRM).toContain("cannot be taken back");
  });
});
