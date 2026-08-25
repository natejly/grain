import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * The thread row's grid must declare a column for every action it can render.
 *
 * jsdom has no layout engine, so this pins the shape of the rule the way the
 * *-layout-css tests do. The bug this guards against was real and invisible in
 * light use: the open thread renders four trailing actions (rename, share,
 * open-in-pane, delete) beside its title, and with one column too few the last
 * button grid-wrapped onto an implicit row BELOW the fixed-height row —
 * overlaying the next thread, which then intercepted every click on Delete.
 * With no thread underneath the click landed fine, so nothing noticed until a
 * crowded rail (or the full e2e run) made the overlap load-bearing.
 */
const css = readFileSync(join(__dirname, "..", "app", "globals.css"), "utf8");
const tsx = readFileSync(
  join(__dirname, "..", "components", "workspace.tsx"),
  "utf8",
);

function threadTemplate(): string {
  const rule = css.match(/\.thread\s*\{([^}]*)\}/);
  expect(rule, ".thread has no rule in globals.css").not.toBeNull();
  const template = rule![1].match(/grid-template-columns:\s*([^;]+);/);
  expect(template, ".thread declares no grid-template-columns").not.toBeNull();
  return template![1].trim();
}

describe("the thread rail row", () => {
  it("declares one trailing column per action button it can render", () => {
    // The actions a single row can carry, counted from the markup: every
    // distinct `thread-<action>` button class rendered inside renderThread.
    // The rename input replaces the whole row, so it does not add a column.
    // Matched loosely (`"thread-share"` also appears inside a ternary), so a
    // button added under a conditional still counts.
    const actions = new Set(
      [...tsx.matchAll(/["'` ]thread-(rename|share|split|delete)["'` ]/g)].map(
        (match) => match[1],
      ),
    );
    const trailingColumns = threadTemplate()
      .split(/\s+/)
      .filter((track) => track === "auto").length;
    expect(trailingColumns).toBe(actions.size);
  });

  it("leads with a shrinkable title track so the actions never overflow", () => {
    expect(threadTemplate().startsWith("minmax(0, 1fr)")).toBe(true);
  });
});
