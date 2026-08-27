import { describe, expect, it } from "vitest";
import { blocksMentioning, css, ruleBody, rulesInside } from "./css-rules";

/**
 * The dashboard tile's chrome is hover-revealed, and the revelation has three
 * load-bearing parts that a stylesheet refactor could silently drop: the fade
 * must be opacity (display:none would yank the buttons out of the tab order,
 * and the resize handle out of the keyboard's reach entirely), focus-within
 * must reveal alongside hover (or a keyboard user tabs onto invisible
 * controls), and the grip must never join the fade — it is the keyboard path
 * to arranging the grid at all.
 */
describe("tile chrome reveals without leaving the tree", () => {
  it("hides the head buttons and resize handle by opacity, not display", () => {
    // The pair share one rule, so read it through the handle and require the
    // buttons to be listed alongside — that survives the two being reordered
    // or respaced, which a literal match of the selector text did not.
    const blocks = blocksMentioning(".tile-resize").filter((body) =>
      /opacity:\s*0/.test(body),
    );
    expect(blocks.length, "nothing fades .tile-resize out").toBe(1);
    expect(css).toMatch(/\.dashboard-pin-head \.icon-button,\s*\.tile-resize\s*\{/);
    expect(blocks[0]).toMatch(/transition:[^;]*opacity/);
    expect(blocks[0]).not.toMatch(/display:/);
  });

  it("reveals on hover and on focus-within alike", () => {
    for (const target of [".dashboard-pin-head .icon-button", ".tile-resize"]) {
      for (const state of [":hover", ":focus-within"]) {
        expect(ruleBody(`.dashboard-pin-tile${state} ${target}`)).toMatch(
          /opacity:\s*1/,
        );
      }
    }
  });

  it("never fades the grip", () => {
    // The grip's own rules say nothing about opacity, and the fade rule's
    // selector list does not sweep it in.
    expect(ruleBody(".tile-grip")).not.toMatch(/opacity/);
  });

  it("keeps the head buttons lit inside the phone breakpoint", () => {
    // Touch has no hover to approach with; the narrow block turns the buttons
    // back on (the grip and resize handle are display:none there already).
    // Asked of the breakpoint itself rather than of a slice starting at a
    // section comment — a comment is not structure, and one rename would have
    // pointed that slice at a different part of the sheet with no test failing.
    const lit = rulesInside(".dashboard-pin-head .icon-button", /max-width/).filter(
      (rule) => /opacity:\s*1/.test(rule.body),
    );
    expect(lit.length, "no narrow-screen rule lights the head buttons").toBeGreaterThan(0);
  });
});
