import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * The thread row's grid must give every trailing action a column of its own,
 * no matter how many actions the row renders.
 *
 * jsdom has no layout engine, so this pins the shape of the rule the way the
 * *-layout-css tests do. The bug this guards against was real and invisible in
 * light use: with fewer declared columns than children the last button
 * grid-wrapped onto an implicit row BELOW the fixed-height row — overlaying
 * the next thread, which then intercepted every click on Delete. With no
 * thread underneath the click landed fine, so nothing noticed until a crowded
 * rail (or the full e2e run) made the overlap load-bearing.
 *
 * The first cure pinned an explicit column count to the action count, with a
 * hardcoded list of action classes — and the four-branch merge beat it: rename
 * (mainline) and comments (feature-sweep) landed on the same row, the new
 * `thread-comments` class was not in the list, and the count stayed one short.
 * The rule now auto-flows implicit COLUMNS instead, which cannot wrap however
 * many actions a merge adds; this test pins that mechanism.
 */
const css = readFileSync(join(__dirname, "..", "app", "globals.css"), "utf8");

function threadRule(): string {
  const rule = css.match(/\.thread\s*\{([^}]*)\}/);
  expect(rule, ".thread has no rule in globals.css").not.toBeNull();
  return rule![1];
}

function declaration(body: string, property: string): string | null {
  const match = body.match(new RegExp(`${property}:\\s*([^;]+);`));
  return match ? match[1].trim() : null;
}

describe("the thread rail row", () => {
  it("auto-flows a column per trailing action so extras can never wrap", () => {
    const body = threadRule();
    expect(declaration(body, "grid-auto-flow")).toBe("column");
    // Without sized implicit columns, auto-flowed actions would land in
    // zero-ambiguity but zero-width tracks on some engines; say the width.
    expect(declaration(body, "grid-auto-columns")).toBe("max-content");
  });

  it("declares only the shrinkable title track explicitly", () => {
    const body = threadRule();
    // Any explicit `auto` action columns beside the title track would reopen
    // the count-vs-children mismatch this file exists to prevent.
    expect(declaration(body, "grid-template-columns")).toBe("minmax(0, 1fr)");
  });
});
