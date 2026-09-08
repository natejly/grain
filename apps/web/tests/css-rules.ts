import { readFileSync } from "node:fs";
import { join } from "node:path";

/**
 * Reading globals.css the way a browser does, for the tests that assert on it.
 *
 * jsdom has no layout engine, so seven suites pin layout by asserting the SHAPE
 * of the rules instead. Each of them grew its own regex matcher, and the
 * differences between those copies were not stylistic — they decided whether a
 * guard could fail at all:
 *
 *  - Three anchored on `(^|[},])`, which cannot match the first rule inside a
 *    `@media` block, because the character before it is `{`. Overrides are
 *    where a phone bug lives, so the guards were blind to exactly the rules
 *    that break them. `collapse-layout-css` knew, and worked around it by
 *    slicing 260 characters forward from the selector; the others did not.
 *  - Two used a non-global `String.match`, returning only the FIRST block for a
 *    selector while the browser applies the union of all of them.
 *  - None stripped comments, so this file's dense prose could be read as a
 *    selector list.
 *
 * Each of those let a real regression through, verified by writing the
 * regression and watching the suite stay green: a re-added `position: absolute`
 * overlay on `.thread-actions`, a phone `.sidebar { height: 100vh }` with no
 * `dvh`, and `.tile-grip { opacity: 0 }` — the grip being the keyboard path to
 * arranging the grid, which the tile suite says in words must never fade.
 *
 * So this parses rather than pattern-matches. It walks the sheet tracking brace
 * depth, which makes nesting a non-issue instead of a regex to get right, and
 * it is one implementation to fix when the next gap turns up.
 */

const source = readFileSync(join(__dirname, "..", "app", "globals.css"), "utf8");

/** The sheet with comments removed — prose must not be mistaken for CSS. */
export const css = source.replace(/\/\*[\s\S]*?\*\//g, "");

export type Rule = {
  /** The selector list, whitespace-collapsed: `.a, .b:hover`. */
  selector: string;
  /** The declarations, without any nested rule. */
  body: string;
  /**
   * The at-rules enclosing this one, outermost first — usually zero or one
   * `@media (max-width: 900px)`.
   *
   * Carried so a test can ask "is this rule inside the phone breakpoint"
   * without slicing the sheet from a comment, which is what several of them
   * used to do. A comment is not structure: renaming a section heading, or
   * stripping comments as this module does, silently changed which rules such
   * a test was looking at.
   */
  context: string[];
};

/**
 * Every style rule in the sheet, at any nesting depth, in file order.
 *
 * At-rules (`@media`, `@supports`, `@keyframes`) are frames rather than rules:
 * they are not returned themselves, and the rules inside them are, which is the
 * whole point — a `@media` override is a rule like any other.
 */
export const rules: Rule[] = (() => {
  const found: Rule[] = [];
  const stack: Array<{ prelude: string; start: number }> = [];
  let mark = 0;
  for (let i = 0; i < css.length; i += 1) {
    const ch = css[i];
    if (ch === "{") {
      stack.push({ prelude: css.slice(mark, i).trim().replace(/\s+/g, " "), start: i + 1 });
      mark = i + 1;
    } else if (ch === "}") {
      const frame = stack.pop();
      if (frame && !frame.prelude.startsWith("@")) {
        found.push({
          selector: frame.prelude,
          body: css.slice(frame.start, i),
          context: stack.map((outer) => outer.prelude).filter((p) => p.startsWith("@")),
        });
      }
      mark = i + 1;
    }
  }
  return found;
})();

/**
 * Does this selector list use `selector` anywhere in it?
 *
 * Found in `.approval-proposal .diff`, in `.diff, .diff-line` and in the
 * compound `.documents-list.collapsed`, but not inside `.diff-line` alone.
 * Descendant and compound uses count, because a declaration written on
 * `.card .diff` reaches the same element a reader is asking about.
 *
 * Only the trailing boundary is required. A leading one would refuse every
 * compound — the character before `.collapsed` in `.documents-list.collapsed`
 * is a word character — and the class's own leading `.` already keeps
 * `.collapsed` out of `.list-collapsed`.
 */
function lists(selectorList: string, selector: string): boolean {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`${escaped}(?![\\w-])`).test(selectorList);
}

/**
 * Every block written for exactly this selector — `.sidebar`, not `.a, .sidebar`.
 *
 * Use this when the assertion is about one block's internal shape, such as a
 * fallback that must be followed by its override. Concatenating would let a
 * correct base rule cover for a broken restatement.
 */
export function blocksFor(selector: string): string[] {
  return rules.filter((rule) => rule.selector === selector).map((rule) => rule.body);
}

/**
 * Every block whose selector list mentions this selector, including as one of
 * several — `.diff` matches `.diff, .diff-line { … }`.
 */
export function blocksMentioning(selector: string): string[] {
  return rules.filter((rule) => lists(rule.selector, selector)).map((rule) => rule.body);
}

/**
 * The declarations a browser would apply for this selector: every mentioning
 * block, concatenated in file order.
 *
 * Right for "is this property set anywhere" and for "is it set NOWHERE"; wrong
 * for anything about one block's internal ordering — see `blocksFor`.
 */
export function ruleBody(selector: string): string {
  return blocksMentioning(selector).join("");
}

/**
 * Every rule mentioning `selector` that sits inside an at-rule matching
 * `context` — `rulesInside(".sidebar", /max-width/)` for the phone overrides.
 */
export function rulesInside(selector: string, context: RegExp): Rule[] {
  return rules.filter(
    (rule) => lists(rule.selector, selector) && rule.context.some((at) => context.test(at)),
  );
}

/** The last value given to `property`, which is the one that wins, or null. */
export function declaration(body: string, property: string): string | null {
  const matches = [
    ...body.matchAll(new RegExp(`(?:^|[;\\s])${property}:\\s*([^;}]+)(?:;|$)`, "g")),
  ];
  return matches.length ? matches[matches.length - 1][1].trim() : null;
}
