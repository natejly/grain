import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * The collapse rules, asserted as text.
 *
 * jsdom has no layout engine, so these pin the facts a browser measurement
 * found — the same reason `projects-layout-css.test.ts` exists. The e2e spec
 * proves the panes actually disappear and come back; this proves the *shape* of
 * the rules, which is where the two ways to get this wrong live: an override
 * that lands before the rule it must beat, and a collapsed pane that takes its
 * own toggle with it.
 */
const css = readFileSync(join(__dirname, "..", "app", "globals.css"), "utf8");

/** Every rule body whose selector list mentions `selector`, concatenated. */
function ruleBody(selector: string): string {
  const pattern = new RegExp(
    `(^|[},])([^{}]*\\${selector}(?![\\w-])[^{}]*)\\{([^}]*)\\}`,
    "g",
  );
  let body = "";
  for (const match of css.matchAll(pattern)) body += match[3];
  return body;
}

/** Where a literal first appears in the sheet — order is what decides a tie. */
function at(needle: string): number {
  const index = css.indexOf(needle);
  expect(index, `${needle} is not in globals.css`).toBeGreaterThan(-1);
  return index;
}

describe("the collapsed rail", () => {
  it("gives the whole grid width to the main panel", () => {
    // Read from the selector forward rather than through `ruleBody`, which
    // anchors on `^ } ,` and so cannot see the first rule inside a media block.
    //
    // Two tracks in the collapsed shell — the icon rail's 56px and the main
    // panel — and the count is the assertion. The CONTEXT sidebar is removed
    // with `display: none`, so it is not a grid item at all; a template that
    // kept a `0` track for it would auto-place <main> into the 0 and the whole
    // app would measure zero wide. That is the bug this pins: the browser
    // rendered a blank window, and the width the spec measures went *down*.
    const rule = css.slice(
      at(".workspace-shell.rail-collapsed {"),
      at(".workspace-shell.rail-collapsed {") + 260,
    );
    expect(rule).toMatch(/grid-template-columns:\s*56px minmax\(0, 1fr\);/);
    // No bare `0` track (the `0` inside minmax() is not a track of its own).
    expect(rule).not.toMatch(/grid-template-columns:[^;]*\s0[\s;]/);
    expect(ruleBody(".rail-collapsed")).toMatch(/display:\s*none/);
  });

  it("stays a desktop idea, so the mobile drawer keeps its own buttons", () => {
    // `display: none` on a fixed-position drawer would take its close button
    // with it, and the topbar toggle is hidden at that width — so the collapse
    // has to be inside a min-width block, not a bare rule.
    const guarded = /@media \(min-width: 901px\) \{[^@]*\.workspace-shell\.rail-collapsed/;
    expect(css).toMatch(guarded);
  });
});

describe("the collapsed list panes", () => {
  it("shrink to a strip rather than to nothing", () => {
    // Not zero: unlike the rail, these two hold their own toggle.
    expect(ruleBody(".documents-layout")).toMatch(/grid-template-columns:\s*44px/);
    expect(ruleBody(".projects-layout")).toMatch(/grid-template-columns:\s*44px/);
  });

  it("keep the toggle and hide everything else", () => {
    const hidden = ruleBody(".collapsed");
    expect(hidden).toMatch(/display:\s*none/);
    // The `:not(.pane-toggle)` is the load-bearing half: without it the head is
    // hidden too and the pane can only be restored from devtools.
    expect(css).toContain(".documents-list.collapsed .documents-list-head > :not(.pane-toggle)");
    expect(css).toContain(
      ".projects-sidebar.collapsed .projects-sidebar-head > :not(.pane-toggle)",
    );
  });

  it("override the with-chat tracks rather than being overridden by them", () => {
    // Same specificity would make source order decide, and the chat grids come
    // first in the sheet.
    expect(at(".documents-layout.with-chat {")).toBeLessThan(
      at(".documents-layout.with-chat.list-collapsed"),
    );
    expect(at(".projects-layout.with-chat {")).toBeLessThan(
      at(".projects-layout.with-chat.list-collapsed"),
    );
  });

  it("turn the strip sideways where the layout stacks", () => {
    // Below 900px these are rows, not columns. Two things go wrong without
    // this: `44px` is a *width* and, being a class more specific than the
    // stacking rule, it wins and leaves a stub column with the editor jammed
    // beside it; and an open pane's 190/220px band survives the collapse, so
    // pressing the toggle gives back no height at all. The block has to sit
    // after the 44px tracks it undoes, which is what slicing from them proves.
    const narrow = css.slice(at(".documents-layout.list-collapsed {"));
    expect(narrow).toMatch(
      /@media \(max-width: 900px\) \{\s*\.documents-layout\.list-collapsed,\s*\.projects-layout\.list-collapsed \{\s*grid-template-columns:\s*1fr;\s*grid-template-rows:\s*auto;\s*grid-auto-rows:\s*minmax\(0, 1fr\);/,
    );
  });

  it("re-state the laptop projects grid inside its own media block", () => {
    // A plain rule after the 1500px block would win at every width and put four
    // columns back on a laptop, which is the bug that block exists to fix.
    // The chat track is the shared `--split-width` token now, not a literal:
    // every subject-chat band reads the one width, so the three cannot drift.
    const narrow = css.slice(at(".projects-layout.with-chat.list-collapsed"));
    expect(narrow).toMatch(
      /@media \(max-width: 1500px\) \{\s*\.projects-layout\.with-chat\.list-collapsed \{\s*grid-template-columns:\s*44px minmax\(0, 1fr\) var\(--split-width\)/,
    );
  });

  it("sizes every subject-chat band from the one shared width token", () => {
    // The point of the token: a hand-tuned literal on one of the three grids
    // is exactly how they drifted to two widths before.
    expect(css).toMatch(/:root \{[^}]*--split-width:\s*340px/);
    for (const layout of [".documents-layout", ".projects-layout", ".dashboards-layout"]) {
      expect(ruleBody(`${layout}.with-chat`)).toMatch(/var\(--split-width\)/);
    }
  });
});
