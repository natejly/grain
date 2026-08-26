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
const workspace = readFileSync(
  join(__dirname, "..", "components", "workspace.tsx"),
  "utf8",
);

function rule(selector: string): string {
  const found = css.match(
    new RegExp(`\\${selector}\\s*\\{([^}]*)\\}`),
  );
  expect(found, `${selector} has no rule in globals.css`).not.toBeNull();
  return found![1];
}

function threadRule(): string {
  return rule(".thread");
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

/**
 * The second half of the same story. Not wrapping was never enough: in flow,
 * every action still spent part of the row's width, and the open row's five of
 * them (six once mainline's favourite star merges) left the title rendering as
 * "Ne…". The cure has to hold for N actions rather than for today's count, so
 * the actions were lifted OUT of flow into one absolutely-positioned cluster.
 * The title track is then the full row width whatever the cluster holds.
 *
 * Two things have to stay true for that, and neither is visible to jsdom:
 * the cluster must be out of flow and confined to its own row, and every
 * action must actually be IN the cluster — an action left outside it would be
 * back in the grid, taking the title's width again.
 */
describe("the thread row's action cluster", () => {
  it("is out of flow, so no number of actions can starve the title", () => {
    const body = rule(".thread-actions");
    expect(declaration(body, "position")).toBe("absolute");
    // Out of flow only stays out of the row below if the row is the containing
    // block and the cluster is no taller than it.
    expect(declaration(threadRule(), "position")).toBe("relative");
    expect(declaration(body, "top")).toBe("0");
    expect(declaration(body, "height")).toBe("100%");
    // A wrapping cluster would grow downward over the next row — the overlap
    // that used to intercept every click on Delete.
    expect(declaration(body, "flex-wrap")).toBe("nowrap");
  });

  it("reveals itself to the mouse AND the keyboard", () => {
    expect(css).toMatch(/\.thread:hover \.thread-actions/);
    expect(css).toMatch(/\.thread:focus-within \.thread-actions/);
    // Touch screens have no hover, so there it never hides.
    expect(css).toMatch(
      /@media \(hover: none\) \{\s*\.thread-actions \{\s*opacity: 1;/,
    );
  });

  it("holds every trailing action the row renders", () => {
    const body = workspace.slice(
      workspace.indexOf("const renderThread"),
      workspace.indexOf("// Per-view badge numbers"),
    );
    expect(body).toContain('className="thread-actions"');
    const clusterAt = body.indexOf('className="thread-actions"');
    // Everything on the row that is not the title button, the space chip or
    // the rename field is a trailing action, and must sit inside the cluster.
    const notActions = new Set([
      "thread-actions",
      "thread-open",
      "thread-space-chip",
      "thread-rename-input",
    ]);
    const classes = [...body.matchAll(/"(thread-[a-z-]+)/g)];
    const actions = classes.filter(([, name]) => !notActions.has(name));
    expect(actions.length).toBeGreaterThanOrEqual(5);
    for (const action of actions) {
      expect(
        action.index! > clusterAt,
        `${action[1]} is rendered outside .thread-actions, which puts it back ` +
          `in the row's grid where it eats the title's width`,
      ).toBe(true);
    }
  });
});
