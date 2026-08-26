import { describe, expect, it } from "vitest";
import { placeLabels, type LabelPlacement } from "../components/graph-3d";

/**
 * Which entity names the 3D canvas gets to show.
 *
 * The force layout positions nodes knowing nothing about how long their names
 * are, so left alone the labels overlap into a heap — worst in exactly the
 * dense cluster you opened the graph to read. `placeLabels` hands out the
 * screen instead: callers pass labels most-mentioned first, and a label whose
 * box is already taken steps aside until the camera moves it clear.
 *
 * The geometry the numbers here assume: a label is LABEL_PIXELS (20) tall plus
 * a 4px gutter on every side, its anchor is its bottom-centre, and its width is
 * 20 * aspect. So an aspect-4 label is 80px wide and claims 88 x 28.
 */
function label(over: Partial<LabelPlacement> = {}): LabelPlacement {
  return { anchorX: 400, anchorY: 300, aspect: 4, behind: false, ...over };
}

const WIDE = 1000;
const TALL = 800;

describe("placing graph labels", () => {
  it("shows a label that has the screen to itself", () => {
    expect(placeLabels([label()], WIDE, TALL)).toEqual([true]);
  });

  it("gives the overlap to whoever is passed first", () => {
    // Same spot, so the second cannot have it. Priority is the caller's order,
    // which is mention count — the hub keeps its name.
    const shown = placeLabels([label(), label()], WIDE, TALL);
    expect(shown).toEqual([true, false]);
  });

  it("lets a label through once it clears the one above it", () => {
    // 28px of claimed height: 20 tall, 4 of gutter each side.
    expect(placeLabels([label(), label({ anchorY: 327 })], WIDE, TALL)).toEqual([true, false]);
    expect(placeLabels([label(), label({ anchorY: 329 })], WIDE, TALL)).toEqual([true, true]);
  });

  it("lets a label through once it clears the one beside it", () => {
    // 88px of claimed width for an aspect-4 label: 80 wide, 4 of gutter each side.
    expect(placeLabels([label(), label({ anchorX: 487 })], WIDE, TALL)).toEqual([true, false]);
    expect(placeLabels([label(), label({ anchorX: 489 })], WIDE, TALL)).toEqual([true, true]);
  });

  it("hides a label the viewport would cut in half", () => {
    // Half off the left edge: drawn, it would read as a truncated name rather
    // than as a label running off the canvas.
    expect(placeLabels([label({ anchorX: 10 })], WIDE, TALL)).toEqual([false]);
    expect(placeLabels([label({ anchorX: 44 })], WIDE, TALL)).toEqual([true]);
    // And the same at the top, where the box grows away from its anchor.
    expect(placeLabels([label({ anchorY: 20 })], WIDE, TALL)).toEqual([false]);
    expect(placeLabels([label({ anchorY: 24 })], WIDE, TALL)).toEqual([true]);
  });

  it("still shows a label too wide to ever fit", () => {
    // A long name in a narrow panel can never sit inside the viewport. Clipped
    // beats a node that can never show its name at all.
    expect(placeLabels([label({ anchorX: 100, aspect: 40 })], 300, TALL)).toEqual([true]);
  });

  it("drops a label behind the camera", () => {
    // A billboarded sprite behind the camera reappears in view; the caller
    // flags it and it never claims a box.
    const shown = placeLabels([label({ behind: true }), label()], WIDE, TALL);
    expect(shown).toEqual([false, true]);
  });

  it("does not let a hidden label reserve space", () => {
    // The one that stepped aside must not block the next in line.
    const shown = placeLabels(
      [label(), label(), label({ anchorX: 700 })],
      WIDE,
      TALL,
    );
    expect(shown).toEqual([true, false, true]);
  });

  it("answers for every label it is given, in order", () => {
    const many = Array.from({ length: 40 }, (_unused, index) =>
      label({ anchorX: 60 + index * 3 }),
    );
    expect(placeLabels(many, WIDE, TALL)).toHaveLength(40);
  });
});
