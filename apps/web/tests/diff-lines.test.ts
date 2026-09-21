import { describe, expect, it } from "vitest";
import { unifiedDiffLines } from "../components/views/diff-lines";

/**
 * The same recognizers proposal-diff.tsx uses, restated here so the round
 * trip — differ output in, red/green rendering out — is pinned without
 * rendering: a `@@` line makes it a diff, and the file header is the pair
 * `--- ` then `+++ `.
 */
function diffStart(lines: readonly string[]): number {
  if (!lines.some((line) => line.startsWith("@@"))) return -1;
  const header = lines.findIndex(
    (line, index) => line.startsWith("--- ") && lines[index + 1]?.startsWith("+++ "),
  );
  return header >= 0 ? header : lines.findIndex((line) => line.startsWith("@@"));
}

const doc = (lines: string[]) => lines.join("\n");

describe("unifiedDiffLines", () => {
  it("returns \"\" for identical inputs — the panel says 'no changes' in words", () => {
    expect(unifiedDiffLines("same\ntext", "same\ntext")).toBe("");
    expect(unifiedDiffLines("", "")).toBe("");
  });

  it("emits a pure insert", () => {
    const before = doc(["a", "b", "c"]);
    const after = doc(["a", "b", "new", "c"]);
    const lines = unifiedDiffLines(before, after).split("\n");
    expect(lines[0]).toBe("--- before");
    expect(lines[1]).toBe("+++ after");
    expect(lines[2]).toBe("@@ -1,3 +1,4 @@");
    expect(lines.slice(3)).toEqual([" a", " b", "+new", " c"]);
  });

  it("emits a pure delete", () => {
    const before = doc(["a", "b", "c"]);
    const after = doc(["a", "c"]);
    const lines = unifiedDiffLines(before, after).split("\n");
    expect(lines[2]).toBe("@@ -1,3 +1,2 @@");
    expect(lines.slice(3)).toEqual([" a", "-b", " c"]);
  });

  it("emits a replace with the - lines before the + lines", () => {
    const lines = unifiedDiffLines("old line", "new line").split("\n");
    expect(lines.slice(2)).toEqual(["@@ -1,1 +1,1 @@", "-old line", "+new line"]);
  });

  it("caps context at 3 lines with correct hunk headers", () => {
    const before = doc(["1", "2", "3", "4", "5", "6", "7", "8", "9", "10"]);
    const after = doc(["1", "2", "3", "4", "5", "CHANGED", "7", "8", "9", "10"]);
    const lines = unifiedDiffLines(before, after).split("\n");
    // 3 context above (lines 3-5), the change at 6, 3 below (7-9).
    expect(lines[2]).toBe("@@ -3,7 +3,7 @@");
    expect(lines.slice(3)).toEqual([
      " 3",
      " 4",
      " 5",
      "-6",
      "+CHANGED",
      " 7",
      " 8",
      " 9",
    ]);
  });

  it("merges hunks whose context would overlap, splits ones far apart", () => {
    const base = Array.from({ length: 30 }, (_, i) => `line ${i + 1}`);
    // Two edits 4 apart (context 3+3 > gap): one hunk.
    const near = [...base];
    near[9] = "edit A";
    near[13] = "edit B";
    const merged = unifiedDiffLines(doc(base), doc(near));
    expect(merged.split("\n").filter((line) => line.startsWith("@@"))).toHaveLength(1);
    // Two edits 15 apart: two hunks.
    const far = [...base];
    far[4] = "edit A";
    far[24] = "edit B";
    const split = unifiedDiffLines(doc(base), doc(far));
    expect(split.split("\n").filter((line) => line.startsWith("@@"))).toHaveLength(2);
  });

  it("diffs the oldest version against \"\" as all-adds", () => {
    const lines = unifiedDiffLines("", doc(["first", "second"])).split("\n");
    expect(lines[2]).toBe("@@ -0,0 +1,2 @@");
    expect(lines.slice(3)).toEqual(["+first", "+second"]);
  });

  it("round-trips through proposal-diff's recognizers", () => {
    const output = unifiedDiffLines("a\nb", "a\nc", {
      from: "Manual edit",
      to: "Agent edit",
    });
    const lines = output.split("\n");
    expect(diffStart(lines)).toBe(0);
    expect(lines[0]).toBe("--- Manual edit");
    expect(lines[1]).toBe("+++ Agent edit");
  });

  it("falls back to a whole-file diff above the line guard, still valid", () => {
    const big = Array.from({ length: 10_100 }, (_, i) => `row ${i}`);
    const changed = [...big];
    changed[0] = "row zero, changed";
    const output = unifiedDiffLines(doc(big), doc(changed));
    const lines = output.split("\n");
    // Still recognized as a diff by the renderer...
    expect(diffStart(lines)).toBe(0);
    // ...and shaped as the documented whole-file replacement.
    expect(lines[2]).toBe("@@ -1,10100 +1,10100 @@");
    expect(lines.filter((line) => line.startsWith("-")).length).toBe(10_101); // 10100 dels + "--- before"
    expect(lines.filter((line) => line.startsWith("+")).length).toBe(10_101);
  });
});
