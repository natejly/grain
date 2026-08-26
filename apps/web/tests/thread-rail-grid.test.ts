import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * Every action the rail row promises is still rendered.
 *
 * This file used to ALSO pin one explicit `auto` grid column per action, and
 * that pin is gone on purpose. It failed the way hardcoded lists fail: the
 * count was kept in step by hand, `thread-comments` was never added to the
 * list, and the merge that brought comments and the favorite star onto the
 * same row left the rule one column short — the last button grid-wrapped onto
 * an implicit row below the fixed-height row, overlaid the next thread, and
 * ate its clicks, with this test green throughout.
 *
 * The rule now auto-flows implicit columns, which cannot wrap however many
 * actions a future merge adds, and `thread-rail-css.test.ts` pins THAT
 * mechanism. What is worth pinning here is only the other half: that the row
 * still offers each action at all.
 */
const shell = readFileSync(
  join(__dirname, "..", "components", "workspace.tsx"),
  "utf8",
);

const ACTIONS = [
  "thread-favorite",
  "thread-rename",
  "thread-share",
  "thread-comments",
  "thread-split",
  "thread-delete",
];

describe("the rail row's actions", () => {
  it("renders every known action button", () => {
    for (const action of ACTIONS) {
      expect(shell.includes(`"${action}`), `${action} missing from the row`).toBe(true);
    }
  });
});
