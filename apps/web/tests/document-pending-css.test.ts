import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * The pending-edit panel in Documents borrows chat's approval-card classes and
 * adds one of its own. jsdom has no layout engine, so these assert the two
 * facts a browser measurement pinned down:
 *
 *  - every class the panel renders actually resolves to a rule (`.document-pending`
 *    shipped with no rule at all, so the panel was unstyled), and
 *  - the panel and its diff are height-bounded and scroll. `.main-panel` clips at
 *    100vh and nothing inside `.document-editor` scrolls, so an unbounded panel
 *    squeezed `.document-panes` to 0px and pushed Approve/Deny past the clip with
 *    no scrollbar to reach them — measured at 43 diff lines in Chromium.
 */
const css = readFileSync(join(__dirname, "..", "app", "globals.css"), "utf8");

function ruleBody(selector: string): string {
  // Every rule whose selector list mentions this exact class, concatenated.
  const pattern = new RegExp(
    `(^|[},])([^{}]*\\${selector}(?![\\w-])[^{}]*)\\{([^}]*)\\}`,
    "g",
  );
  let body = "";
  for (const match of css.matchAll(pattern)) body += match[3];
  return body;
}

describe("documents pending-edit panel styling", () => {
  it("has a rule for every class the panel renders", () => {
    const rendered = [
      ".document-pending",
      ".tool-card",
      ".tool-card-head",
      ".tool-card-approval",
      ".tool-card-actions",
      ".tool-error",
      ".diff",
      ".diff-line",
      ".proposal-note",
      ".ghost-button",
      ".primary-button",
    ];
    for (const selector of rendered) {
      expect(ruleBody(selector), `${selector} has no rule`).not.toBe("");
    }
  });

  it("bounds the panel height and gives it a scrollbar", () => {
    const body = ruleBody(".document-pending");
    expect(body).toMatch(/max-height:/);
    expect(body).toMatch(/overflow-y:\s*(auto|scroll)/);
  });

  it("bounds the diff so the approve and deny buttons stay in view", () => {
    const body = ruleBody(".diff");
    expect(body).toMatch(/max-height:/);
    expect(body).toMatch(/overflow-y:\s*(auto|scroll)/);
  });
});
