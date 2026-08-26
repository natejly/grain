import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * The thread row's title must survive however many actions the row can offer.
 *
 * This has been fixed three times, and the first two cures were both about
 * POSITION — which was never the variable that mattered:
 *
 *   1. An explicit `auto` grid column per action. The count was kept in step by
 *      hand, a merge added `thread-comments` without adding a column, and the
 *      overflowing button wrapped onto an implicit row inside the fixed-height
 *      row, overlaid the next thread and ate its clicks.
 *   2. Auto-flowed columns, then the whole cluster taken out of flow. Nothing
 *      could wrap any more — but in flow N actions still divided the 227px row
 *      and out of flow they covered it. Measured at six actions the title got
 *      35px, "Quar…", on precisely the row the reader had just opened.
 *
 * The actions are a MENU now, and that is the first arrangement where the count
 * cannot reach the title at all: a seventh action is a seventh row in the
 * panel. So this file no longer pins a layout — it pins the property that makes
 * the layout unnecessary, which is that the row renders exactly one trailing
 * control and every action lives inside the disclosure.
 *
 * jsdom has no layout engine, so the CSS half is asserted as rule shape, the
 * way the other *-css tests do.
 */
const css = readFileSync(join(__dirname, "..", "app", "globals.css"), "utf8");
const workspace = readFileSync(
  join(__dirname, "..", "components", "workspace.tsx"),
  "utf8",
);

function rule(selector: string): string {
  const found = css.match(new RegExp(`\\${selector}\\s*\\{([^}]*)\\}`));
  expect(found, `${selector} has no rule in globals.css`).not.toBeNull();
  return found![1];
}

function declaration(body: string, property: string): string | null {
  const match = body.match(new RegExp(`(?:^|[;\\s])${property}:\\s*([^;]+);`));
  return match ? match[1].trim() : null;
}

/** Every action the row can offer, by the class each one carries. */
const ACTIONS = [
  "thread-favorite",
  "thread-rename",
  "thread-share",
  "thread-split",
  "thread-comments",
  "thread-delete",
];

describe("the thread row's actions", () => {
  it("puts every action inside the disclosure, not on the row", () => {
    // The guard that matters: an action added later must land in the panel.
    // Rendered on the row it would take width from the title again, which is
    // the whole defect this arrangement exists to end.
    const menu = workspace.slice(
      workspace.indexOf("<DisclosureMenu"),
      workspace.indexOf("</DisclosureMenu>"),
    );
    expect(menu.length, "the rail renders no DisclosureMenu").toBeGreaterThan(0);
    for (const action of ACTIONS) {
      expect(
        menu.includes(action),
        `${action} is not inside the row's DisclosureMenu`,
      ).toBe(true);
    }
  });

  it("leaves exactly one trailing control on the row itself", () => {
    const row = workspace.slice(
      workspace.indexOf('<div className="thread-actions">'),
      workspace.indexOf("<DisclosureMenu"),
    );
    // Between the cluster opening and the menu there is nothing but the
    // trigger — no action may be promoted back onto the row without this
    // failing, whatever its author's reason.
    expect((row.match(/<button/g) ?? []).length).toBe(0);
    expect(workspace).toContain('triggerClassName="thread-more"');
  });

  it("keeps the title track shrinkable", () => {
    const thread = rule(".thread");
    // One shrinkable title column. The first cure's `auto` action columns would
    // reopen the count-vs-width coupling this file exists to prevent.
    expect(declaration(thread, "grid-template-columns")).toBe("minmax(0, 1fr)");
  });

  it("no longer hides a hittable strip over the row", () => {
    // The cluster used to be `opacity: 0` with `pointer-events: none` at rest,
    // which does NOT make a no-hover click safe: the pointer's ARRIVAL fires
    // :hover and restores pointer-events before the button-down lands, so a
    // click at the row's right edge fired Delete. Verified by dispatching real
    // events, not by hit-testing a stationary point, which reports the wrong
    // answer because a pointer is never stationary when it presses. One
    // always-visible trigger has no such state to be caught in.
    const actions = rule(".thread-actions");
    expect(declaration(actions, "opacity")).toBeNull();
    expect(declaration(actions, "pointer-events")).toBeNull();
    expect(declaration(actions, "position")).not.toBe("absolute");
  });

  it("marks the destructive row as destructive", () => {
    const danger = rule(".disclosure-option.danger");
    expect(declaration(danger, "color")).toBe("var(--danger)");
    expect(workspace).toContain("thread-delete danger");
  });
});
