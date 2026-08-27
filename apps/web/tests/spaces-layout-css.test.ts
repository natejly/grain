import { describe, expect, it } from "vitest";
import { ruleBody } from "./css-rules";

/**
 * The spaces view is a two-pane grid inside the main panel's flex column.
 * The flex height chain breaks silently — a missing `min-height: 0` shows up
 * as a page that scrolls as a whole instead of the two panes scrolling
 * independently — so the invariants are pinned the way the other layouts pin
 * theirs.
 */

describe("spaces layout fills the main panel", () => {
  it(".spaces-layout participates in flex and has min-height: 0", () => {
    const body = ruleBody(".spaces-layout");
    expect(body).toMatch(/flex:\s*1/);
    expect(body).toMatch(/min-height:\s*0/);
  });

  it(".spaces-list scrolls on its own", () => {
    const body = ruleBody(".spaces-list");
    expect(body).toMatch(/overflow-y:\s*auto/);
    expect(body).toMatch(/min-height:\s*0/);
  });

  it(".space-detail scrolls on its own", () => {
    const body = ruleBody(".space-detail");
    expect(body).toMatch(/overflow-y:\s*auto/);
    expect(body).toMatch(/min-height:\s*0/);
  });
});
