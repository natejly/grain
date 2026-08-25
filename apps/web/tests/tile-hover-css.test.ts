import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * The dashboard tile's chrome is hover-revealed, and the revelation has three
 * load-bearing parts that a stylesheet refactor could silently drop: the fade
 * must be opacity (display:none would yank the buttons out of the tab order,
 * and the resize handle out of the keyboard's reach entirely), focus-within
 * must reveal alongside hover (or a keyboard user tabs onto invisible
 * controls), and the grip must never join the fade — it is the keyboard path
 * to arranging the grid at all.
 */
const css = readFileSync(join(__dirname, "..", "app", "globals.css"), "utf8");

function ruleBody(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const pattern = new RegExp(`(^|[},])([^{}]*${escaped}(?![\\w-])[^{}]*)\\{([^}]*)\\}`, "g");
  let body = "";
  for (const match of css.matchAll(pattern)) body += match[3];
  return body;
}

describe("tile chrome reveals without leaving the tree", () => {
  it("hides the head buttons and resize handle by opacity, not display", () => {
    const pattern =
      /\.dashboard-pin-head \.icon-button,\s*\.tile-resize\s*\{([^}]*)\}/;
    const body = css.match(pattern)?.[1] ?? "";
    expect(body).toMatch(/opacity:\s*0/);
    expect(body).toMatch(/transition:[^;]*opacity/);
    expect(body).not.toMatch(/display:/);
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
    // Touch has no hover to approach with; the 900px block turns the buttons
    // back on (the grip and resize handle are display:none there already).
    const mobile = css.slice(css.indexOf("Twelve columns in 380px"));
    expect(mobile).toMatch(
      /\.dashboard-pin-head \.icon-button\s*\{\s*opacity:\s*1;\s*\}/,
    );
  });
});
