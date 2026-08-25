import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * The rail row's grid must hold one `auto` column per action button, and this
 * has now bitten twice: the rename button and then the favorite star each
 * landed without a column, wrapped onto a second grid row inside the fixed
 * 34px height, and bled over the neighboring thread — which then intercepted
 * clicks meant for this row's own buttons. Every gate was green both times;
 * only a full e2e run showed it.
 *
 * So the invariant is pinned structurally: the number of `.thread-*` action
 * classes the shell renders and the number of `auto` columns in the `.thread`
 * rule must agree. A merge that resolves the grid toward a branch with fewer
 * actions (bg/marketplace-todo pinned FOUR columns before the star existed)
 * fails here instead of shipping the click-bleed back.
 */
const css = readFileSync(join(__dirname, "..", "app", "globals.css"), "utf8");
const shell = readFileSync(
  join(__dirname, "..", "components", "workspace.tsx"),
  "utf8",
);

const ACTIONS = [
  "thread-favorite",
  "thread-rename",
  "thread-share",
  "thread-split",
  "thread-delete",
];

describe("the rail row's grid and its actions agree", () => {
  it("renders exactly the five known action buttons", () => {
    for (const action of ACTIONS) {
      expect(shell.includes(`"${action}`), `${action} missing from the row`).toBe(true);
    }
  });

  it("gives the .thread grid one auto column per action", () => {
    const rule = css.match(/\.thread\s*\{[^}]*\}/)?.[0] ?? "";
    const columns = rule.match(/grid-template-columns:\s*([^;]+);/)?.[1] ?? "";
    const autos = columns.match(/\bauto\b/g)?.length ?? 0;
    expect(columns).toMatch(/minmax\(0, 1fr\)/);
    expect(autos, `.thread has ${autos} auto columns for ${ACTIONS.length} actions`).toBe(
      ACTIONS.length,
    );
  });
});
