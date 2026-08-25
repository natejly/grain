import { describe, expect, it } from "vitest";
import type { Conversation } from "@workspace/api-client";
import { buildPaletteRows, matchPalette } from "../components/views/palette";

/**
 * The palette's contract: everything reachable, nothing exclusive, ranked so
 * the row you meant is the row that is focused. The component is a thin shell
 * over these two functions, which is why they carry the tests.
 */

function thread(id: string, title: string, shared = false): Conversation {
  return {
    id,
    title,
    subject_kind: "",
    subject_id: "",
    space_id: "",
    default_agent_id: "",
    default_model: "",
    default_effort: "",
    approval_mode: "ask_writes",
    shared,
    owned: true,
    can_share: true,
    created_at: "2026-08-19T00:00:00Z",
    updated_at: "2026-08-19T00:00:00Z",
  };
}

describe("buildPaletteRows", () => {
  it("carries every navigable view, settings surfaces included", () => {
    const rows = buildPaletteRows([]);
    const labels = rows.filter((row) => row.kind === "view").map((row) => row.label);
    // One from each altitude: a rail landing view, a Library shelf item, and a
    // settings surface — the palette is how the far ones stay near.
    for (const expected of ["Chat", "Memory", "MCP servers", "Admin", "Inbox"]) {
      expect(labels).toContain(expected);
    }
  });

  it("offers exactly the Create menu's actions, so the surfaces cannot drift", () => {
    const rows = buildPaletteRows([]);
    const creates = rows.filter((row) => row.kind === "create").map((row) => row.label);
    expect(creates).toEqual([
      "New document",
      "New project",
      "New LaTeX document",
      "New board",
      "New app",
      "New workflow",
    ]);
  });

  it("lists threads by title with a shared/personal hint", () => {
    const rows = buildPaletteRows([thread("c1", "Quarterly digest", true)]);
    const row = rows.find((item) => item.kind === "thread");
    expect(row?.label).toBe("Quarterly digest");
    expect(row?.hint).toBe("Shared thread");
  });
});

describe("matchPalette", () => {
  const rows = buildPaletteRows([
    thread("c1", "Launch runbook review"),
    thread("c2", "Budget questions"),
  ]);

  it("answers the empty query with navigation, not the thread list", () => {
    // "What can I even do" is the empty palette's question; an unfiltered
    // thread list is what the rail already is.
    const matches = matchPalette(rows, "");
    expect(matches.length).toBeGreaterThan(0);
    expect(matches.every((row) => row.kind !== "thread")).toBe(true);
  });

  it("ranks prefix over word-start over substring", () => {
    const matches = matchPalette(rows, "da");
    const labels = matches.map((row) => row.label);
    // "Dashboards" and "Datasets" start with the query; anything merely
    // containing "da" must come after them.
    expect(labels[0].toLowerCase().startsWith("da")).toBe(true);
    const firstSubstringOnly = labels.findIndex(
      (label) =>
        !label.toLowerCase().startsWith("da") &&
        !label.toLowerCase().split(/\s+/).some((word) => word.startsWith("da")),
    );
    const lastPrefix = labels
      .map((label) => label.toLowerCase().startsWith("da"))
      .lastIndexOf(true);
    if (firstSubstringOnly !== -1) {
      expect(lastPrefix).toBeLessThan(firstSubstringOnly);
    }
  });

  it("finds a thread by any word in its title", () => {
    const matches = matchPalette(rows, "runbook");
    expect(matches.some((row) => row.kind === "thread" && row.label.includes("runbook"))).toBe(
      true,
    );
  });

  it("is case-insensitive and bounded", () => {
    expect(matchPalette(rows, "BUDGET")[0].label).toBe("Budget questions");
    expect(matchPalette(rows, "", 3)).toHaveLength(3);
  });
});
