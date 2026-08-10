import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * The theme rests on one invariant: colour lives in the three token blocks at
 * the top of globals.css and nowhere else. A raw hex in a rule below them is a
 * rule that can only be right in one theme, and it will look broken in the
 * other with nothing failing — which is exactly how the dark-only build ended
 * up with a white project preview and a navy 2D graph.
 *
 * These assert that invariant, that the three blocks agree on which tokens
 * exist (a token defined only under a media query falls back to nothing), and
 * that the WCAG AA ratios the palette was chosen for still hold.
 */
const css = readFileSync(join(__dirname, "..", "app", "globals.css"), "utf8");

/** The declaration body of each of the three token blocks, in source order. */
function themeBlocks(): { name: string; body: string; end: number }[] {
  const heads = [
    { name: ":root (light)", marker: "\n:root {\n  color-scheme: light;" },
    { name: "prefers-color-scheme: dark", marker: '  :root:not([data-theme="light"]) {' },
    { name: '[data-theme="dark"]', marker: '\n:root[data-theme="dark"] {' },
  ];
  return heads.map(({ name, marker }) => {
    const start = css.indexOf(marker);
    expect(start, `missing theme block: ${name}`).toBeGreaterThan(-1);
    // The dark media block nests, so scan braces rather than looking for "\n}".
    const open = css.indexOf("{", start);
    let depth = 0;
    let cursor = open;
    for (; cursor < css.length; cursor += 1) {
      if (css[cursor] === "{") depth += 1;
      else if (css[cursor] === "}" && (depth -= 1) === 0) break;
    }
    return { name, body: css.slice(open + 1, cursor), end: cursor };
  });
}

function tokensIn(body: string): string[] {
  return [...body.matchAll(/^\s*(--[\w-]+):/gm)].map((match) => match[1]).sort();
}

/** WCAG 2.x relative luminance. */
function luminance(hex: string): number {
  const channel = (value: number) => {
    const c = value / 255;
    return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
  };
  const n = parseInt(hex.slice(1), 16);
  return (
    0.2126 * channel((n >> 16) & 255) +
    0.7152 * channel((n >> 8) & 255) +
    0.0722 * channel(n & 255)
  );
}

function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

function tokenValue(body: string, token: string): string {
  const match = body.match(new RegExp(`^\\s*${token}:\\s*([^;]+);`, "m"));
  expect(match, `${token} is not defined in this block`).not.toBeNull();
  return match![1].trim();
}

describe("theme token architecture", () => {
  it("declares no colour outside the token blocks", () => {
    const blocks = themeBlocks();
    const rules = css.slice(Math.max(...blocks.map((block) => block.end)));
    const literals = rules.match(/#[0-9a-fA-F]{3,8}\b|\brgba?\(/g);
    expect(literals ?? [], `hardcoded colours below the token blocks: ${literals}`)
      .toHaveLength(0);
  });

  it("defines the same token set in all three blocks", () => {
    const [light, media, explicit] = themeBlocks().map((block) => tokensIn(block.body));
    expect(media).toEqual(light);
    expect(explicit).toEqual(light);
  });

  it("switches color-scheme with the theme", () => {
    const [light, media, explicit] = themeBlocks();
    expect(light.body).toContain("color-scheme: light");
    expect(media.body).toContain("color-scheme: dark");
    expect(explicit.body).toContain("color-scheme: dark");
  });

  it("keeps the two dark blocks byte-identical", () => {
    // They are duplicated on purpose — a token defined only inside the media
    // query would vanish for a user who toggled dark on a light system — so
    // the risk is editing one and forgetting the other.
    const [, media, explicit] = themeBlocks();
    const normalise = (body: string) => body.replace(/^\s+/gm, "").trim();
    expect(normalise(media.body)).toEqual(normalise(explicit.body));
  });
});

describe("WCAG AA contrast", () => {
  // Body text at 4.5:1 against every surface the token is actually painted on.
  const TEXT_PAIRS: [string, string][] = [
    ["--text", "--bg"],
    ["--text", "--sidebar"],
    ["--text", "--surface"],
    ["--text", "--surface-raised"],
    ["--text", "--surface-hover"],
    ["--text", "--code-bg"],
    ["--text-muted", "--bg"],
    ["--text-muted", "--sidebar"],
    ["--text-muted", "--surface"],
    ["--text-muted", "--surface-hover"],
    ["--text-dim", "--bg"],
    ["--text-dim", "--sidebar"],
    ["--text-dim", "--surface"],
    ["--text-dim", "--surface-raised"],
    ["--text-dim", "--surface-hover"],
    ["--text-dim", "--accent-muted"],
    ["--accent", "--bg"],
    ["--accent", "--surface"],
    ["--accent", "--sidebar"],
    ["--accent-text", "--accent-muted"],
    ["--accent-text", "--accent-wash"],
    ["--accent-text", "--accent-muted-hover"],
    ["--accent-contrast", "--accent"],
    ["--accent-contrast", "--accent-hover"],
    ["--green", "--green-muted"],
    ["--amber", "--amber-muted"],
    ["--amber", "--clay-muted"],
    ["--red", "--red-muted"],
    ["--graph-label", "--bg"],
  ];

  for (const { name, body } of themeBlocks()) {
    describe(name, () => {
      for (const [fg, bg] of TEXT_PAIRS) {
        it(`${fg} on ${bg} reaches 4.5:1`, () => {
          const ratio = contrast(tokenValue(body, fg), tokenValue(body, bg));
          expect(Number(ratio.toFixed(2))).toBeGreaterThanOrEqual(4.5);
        });
      }

      it("--clay reads as a boundary at 3:1", () => {
        expect(contrast(tokenValue(body, "--clay"), tokenValue(body, "--bg")))
          .toBeGreaterThanOrEqual(3);
      });
    });
  }
});
