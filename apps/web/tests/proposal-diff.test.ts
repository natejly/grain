import { cleanup, render } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { ProposalDiff } from "../components/views/proposal-diff";

afterEach(cleanup);

/** The rendered lines, as `[class, text]`, which is what the colours are. */
function diffLines(container: HTMLElement): [string, string][] {
  return Array.from(container.querySelectorAll(".diff-line")).map((node) => [
    node.className.replace("diff-line ", ""),
    node.textContent ?? "",
  ]);
}

const DIFF = [
  "--- Revenue (current)",
  "+++ Revenue (proposed)",
  "@@ -1,3 +1,3 @@",
  " {",
  '-  "visualization": "table",',
  '+  "visualization": "bar",',
  " }",
].join("\n");

describe("ProposalDiff", () => {
  it("colours a unified diff line by line", () => {
    const { container } = render(createElement(ProposalDiff, { preview: DIFF }));
    expect(diffLines(container)).toEqual([
      ["file", "--- Revenue (current)"],
      ["file", "+++ Revenue (proposed)"],
      ["hunk", "@@ -1,3 +1,3 @@"],
      ["ctx", " {"],
      ["del", '-  "visualization": "table",'],
      ["add", '+  "visualization": "bar",'],
      ["ctx", " }"],
    ]);
    // A bare diff is all diff: no sentence was offered, so none is invented.
    expect(container.querySelector(".proposal-note")).toBeNull();
  });

  it("keeps a leading sentence out of the diff instead of shading it as context", () => {
    // A dashboard edit previews as a summary with the spec diff under it. The
    // bug this pins: matching "is there a @@ anywhere" and then colouring every
    // line, which paints the sentence as three grey context lines and makes the
    // one thing a reviewer reads first look like unchanged JSON.
    const preview = `Update dashboard “Revenue”: it shows a table, and would show a bar.\n\n${DIFF}`;
    const { container } = render(createElement(ProposalDiff, { preview }));
    expect(container.querySelector(".proposal-note")?.textContent).toBe(
      "Update dashboard “Revenue”: it shows a table, and would show a bar.",
    );
    expect(diffLines(container)[0]).toEqual(["file", "--- Revenue (current)"]);
    expect(diffLines(container)).toHaveLength(7);
  });

  it("renders a preview that is not a diff as prose, dashes and all", () => {
    const { container } = render(
      createElement(ProposalDiff, { preview: "Move “Ship it” from Todo — to Done" }),
    );
    expect(container.querySelector(".proposal-note")?.textContent).toBe(
      "Move “Ship it” from Todo — to Done",
    );
    expect(diffLines(container)).toEqual([]);
  });

  it("still finds a diff that arrives without file headers", () => {
    const preview = "@@ -1 +1 @@\n-old\n+new";
    const { container } = render(createElement(ProposalDiff, { preview }));
    expect(diffLines(container)).toEqual([
      ["hunk", "@@ -1 +1 @@"],
      ["del", "-old"],
      ["add", "+new"],
    ]);
    expect(container.querySelector(".proposal-note")).toBeNull();
  });
});
