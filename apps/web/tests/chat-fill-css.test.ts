import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * The chat fills the panel it is given, and the shell is as tall as the
 * viewport actually is.
 *
 * jsdom has no layout engine, so these pin the shape of the rules the way the
 * *-layout-css tests do. Two bugs are guarded here, both of which only showed
 * up on a device:
 *
 *  1. The transcript, the composer, the composer's picker shell and the bypass
 *     banner used to hard-code `min(760px, 100%)` each. Four copies of one
 *     measure drift: the picker floated off-centre from the box it belongs to
 *     the moment one of them changed. They now read a single token.
 *
 *  2. `100vh` on a phone is the viewport with the browser toolbars retracted —
 *     taller than what is on screen — so a `100vh` shell pushed the composer
 *     below the fold. Every full-height surface needs the `dvh` line, and it
 *     has to come *after* the `vh` line, which is the fallback.
 */
const css = readFileSync(join(__dirname, "..", "app", "globals.css"), "utf8");

function rule(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = css.match(new RegExp(`(?:^|\\})\\s*${escaped}\\s*\\{([^}]*)\\}`, "m"));
  expect(match, `${selector} has no rule in globals.css`).not.toBeNull();
  return match![1];
}

describe("the chat measure", () => {
  it("is declared once, on the layout that owns it", () => {
    expect(rule(".chat-layout")).toMatch(/--chat-measure:\s*100%/);
  });

  it("is what every element on that measure reads", () => {
    for (const selector of [
      ".message-column",
      ".composer",
      ".composer-shell",
      ".bypass-banner",
    ]) {
      expect(rule(selector), selector).toMatch(
        /width:\s*var\(--chat-measure,\s*100%\)/,
      );
    }
  });

  it("leaves no hard-coded copy of the old centred column behind", () => {
    // The four rules above were each `min(760px, 100%)`. Any survivor is an
    // element that stayed a centred island while the rest went full width.
    expect(css).not.toContain("min(760px");
  });
});

describe("full-height surfaces", () => {
  // Every surface that is meant to be exactly as tall as the window.
  const surfaces: Array<[string, "height" | "min-height"]> = [
    [".workspace-shell", "min-height"],
    [".icon-rail", "height"],
    [".sidebar", "height"],
    [".main-panel", "height"],
    [".auth-shell", "min-height"],
  ];

  for (const [selector, property] of surfaces) {
    it(`${selector} sizes to the *visible* viewport`, () => {
      const body = rule(selector);
      const declarations = [...body.matchAll(new RegExp(`${property}:\\s*100(dvh|vh)`, "g"))].map(
        (match) => match[1],
      );
      // Both, and in this order: `vh` is the fallback for a browser that does
      // not know `dvh`, so a `dvh` line placed first would be overwritten.
      expect(declarations, `${selector} ${property}`).toEqual(["vh", "dvh"]);
    });
  }
});

describe("the composer on a phone", () => {
  it("clears the iOS home indicator in every rule that sets its bottom padding", () => {
    // `.composer-zone` sets the inset in its base rule; the phone block
    // restates the shorthand, and a restated shorthand that forgets the inset
    // puts the send button under the home indicator.
    const zones = [...css.matchAll(/\.composer-zone\s*\{([^}]*)\}/g)]
      .map((match) => match[1])
      .filter((body) => /padding:/.test(body));
    expect(zones.length).toBeGreaterThanOrEqual(2);
    for (const body of zones) {
      expect(body).toMatch(/safe-area-inset-bottom/);
    }
  });

  it("forces 16px form control text hard enough to actually win", () => {
    // The guard used to lean on the `.workspace-shell` ancestor for
    // specificity, which is (0,1,1) — beaten by `.composer-tools
    // .composer-select` (0,2,0) and `.composer-tools .agent-chip
    // .agent-select` (0,3,0). Both dropdowns still zoomed iOS in, and iOS does
    // not zoom back out. There is no ancestor chain that outranks an arbitrary
    // descendant selector, so the rule stops competing on specificity.
    const guard = css.match(
      /\.workspace-shell input,\s*\.workspace-shell select,\s*\.workspace-shell textarea\s*\{([^}]*)\}/,
    );
    expect(guard, "the iOS zoom guard is gone").not.toBeNull();
    expect(guard![1]).toMatch(/font-size:\s*16px\s*!important/);
  });

  it("lets the composer's control row wrap rather than clipping it", () => {
    // Eight chips is ~560px of controls in a ~380px composer. The desktop rule
    // pins the row at 36px, which clipped everything past the third chip —
    // including Send — with no scrollbar to say so.
    const mobile = css.slice(css.indexOf("Mobile refinements"));
    const tools = mobile.match(/\.composer-tools\s*\{([^}]*)\}/);
    expect(tools, "no mobile .composer-tools rule").not.toBeNull();
    expect(tools![1]).toMatch(/flex-wrap:\s*wrap/);
    expect(tools![1]).toMatch(/height:\s*auto/);
  });
});
