import { describe, expect, it } from "vitest";
import { UNDO_CONFIRM, summarizeUndo } from "../components/views/undo-format";

const reverted = (tool: string) => ({ tool_name: tool, kind: "document" });
const skipped = (tool: string, reason = "external effects cannot be undone") => ({
  tool_name: tool,
  reason,
});

describe("summarizeUndo", () => {
  it("stays silent when everything reverted cleanly", () => {
    expect(
      summarizeUndo({ run_id: "r1", reverted: [reverted("edit_document")], skipped: [] }),
    ).toBe("");
  });

  it("stays silent for a run that recorded nothing to undo", () => {
    expect(summarizeUndo({ run_id: "r1", reverted: [], skipped: [] })).toBe("");
  });

  it("names the tools whose effects could not be taken back", () => {
    const summary = summarizeUndo({
      run_id: "r1",
      reverted: [reverted("edit_document"), reverted("board_add_card")],
      skipped: [skipped("run_python")],
    });
    expect(summary).toBe(
      "Undid 2 changes; 1 change could not be reverted (run_python).",
    );
  });

  it("says plainly when nothing at all could be undone", () => {
    const summary = summarizeUndo({
      run_id: "r1",
      reverted: [],
      skipped: [skipped("run_python"), skipped("sql_execute")],
    });
    expect(summary).toBe(
      "Nothing could be undone; 2 changes could not be reverted (run_python, sql_execute).",
    );
  });

  it("uses the singular for a single reverted change", () => {
    const summary = summarizeUndo({
      run_id: "r1",
      reverted: [reverted("edit_document")],
      skipped: [skipped("run_command")],
    });
    expect(summary).toBe(
      "Undid 1 change; 1 change could not be reverted (run_command).",
    );
  });

  it("warns about external effects in the confirm copy", () => {
    expect(UNDO_CONFIRM).toContain("cannot be taken back");
  });
});
