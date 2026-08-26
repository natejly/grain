import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * A claim has to be visible on BOTH surfaces that draw a `board_cards` row.
 *
 * A todo list is a board with one column, so a checklist item and a kanban
 * card are literally the same row wearing different chrome. The claim lease
 * lives on that row, which means an agent can take a card and — before this
 * was pinned — have nowhere to say so on the kanban view, while the identical
 * card in a checklist showed a chip. That gap is invisible to a type checker
 * and to every unit test of the component itself, because the component was
 * fine; it simply was not rendered in one of the two places.
 *
 * Structural assertions over the sources rather than a render test, matching
 * how the other layout invariants here are pinned: the failure mode is "a
 * surface forgot to mount it", which is a fact about the call sites.
 */
const components = join(__dirname, "..", "components", "views");
const board = readFileSync(join(components, "board.tsx"), "utf8");
const todos = readFileSync(join(components, "todos.tsx"), "utf8");
const css = readFileSync(join(__dirname, "..", "app", "globals.css"), "utf8");

describe("the claim affordance reaches both card surfaces", () => {
  it("is one exported component, not two copies", () => {
    // Two implementations would drift on the part that matters most: which
    // verb a non-holder is offered, and therefore whether taking a card from
    // somebody is the deliberate `force` it is meant to be.
    expect(todos).toMatch(/export function ClaimBadge\(/);
    expect(board).toMatch(/import \{[^}]*\bClaimBadge\b[^}]*\} from "\.\/todos"/);
    expect(board).not.toMatch(/function ClaimBadge\(/);
  });

  it("mounts on the kanban card", () => {
    expect(board).toMatch(/<ClaimBadge/);
  });

  it("still mounts on the checklist item", () => {
    expect(todos).toMatch(/<ClaimBadge/);
  });

  it("gives the kanban card its own hover target for a free claim", () => {
    // `.todo-claim.free` is hidden until revealed, and the checklist reveals
    // it via `.todo-item:hover` — a selector that never matches inside a
    // kanban card. Without a card-level rule the Claim button is present,
    // focusable and permanently invisible to a mouse.
    expect(css).toMatch(/\.kanban-card:hover \.todo-claim\.free/);
  });

  it("keeps the keyboard reveal unscoped", () => {
    // Load-bearing by accident, and one tidy-up away from an a11y bug that
    // does not currently exist: `.todo-claim.free:focus-visible` has no
    // ancestor, so it reveals a free claim on EVERY surface the component
    // mounts on. Scoping it under `.todo-item` for symmetry with the hover
    // half beside it would make the Claim button reachable by Tab and
    // invisible once reached, everywhere that is not a checklist.
    //
    // Asserted as "appears with no ancestor" rather than by matching the
    // whole rule, so reformatting the stylesheet cannot fail this while
    // re-scoping it — the actual thing being protected — still does.
    expect(css).toMatch(/(^|[,{}\s])\.todo-claim\.free:focus-visible/m);
    expect(css).not.toMatch(/\.todo-item[^,{}]*\.todo-claim\.free:focus-visible/);
  });
});
