import type { CoworkingPresence } from "@workspace/api-client";
import { describe, expect, it } from "vitest";
import { actorHue, pointing } from "../components/live-cursors";

/**
 * The pure core of the live pointer layer.
 *
 * Two claims carry the whole "I can see you move" feel and both are pinned
 * here: a person is the SAME colour in everyone's browser (otherwise "the blue
 * cursor" is not a thing two people can say to each other), and a presence
 * with nothing drawable never becomes a cursor at the origin — the failure
 * that reads to a viewer as a stranger parked in the top-left corner.
 */

function presence(overrides: Partial<CoworkingPresence>): CoworkingPresence {
  return {
    actor_id: "u1",
    actor_kind: "user",
    actor_label: "Sam",
    surface: "document:d1",
    state: {},
    updated_at: "2026-08-25T10:00:00Z",
    ...overrides,
  };
}

describe("actorHue", () => {
  it("gives one actor the same hue every time", () => {
    expect(actorHue("user-abc")).toBe(actorHue("user-abc"));
  });

  it("stays inside the hue circle", () => {
    for (const id of ["", "a", "user-abc", "9f2c".repeat(20), "ünïcödé"]) {
      const hue = actorHue(id);
      expect(hue).toBeGreaterThanOrEqual(0);
      expect(hue).toBeLessThan(360);
      expect(Number.isInteger(hue)).toBe(true);
    }
  });

  it("does not collapse a realistic set of ids onto one colour", () => {
    // Not a uniqueness claim — 360 hues and a hash will collide eventually.
    // The claim is that a normal room does not come out monochrome.
    const hues = new Set(
      Array.from({ length: 12 }, (_, i) => actorHue(`3f8a-user-${i}`)),
    );
    expect(hues.size).toBeGreaterThan(8);
  });
});

describe("pointing", () => {
  it("keeps a presence carrying a pointer", () => {
    const rows = [presence({ state: { pointer: { x: 0.5, y: 0.25 } } })];
    expect(pointing(rows)).toHaveLength(1);
  });

  it("drops a presence with no pointer at all", () => {
    // The common case by far: someone reading, or typing without a mouse.
    expect(pointing([presence({ state: { typing: true } })])).toEqual([]);
  });

  it.each([
    ["a half pointer", { x: 0.5 }],
    ["strings", { x: "0.5", y: "0.5" }],
    ["nulls", { x: null, y: null }],
    ["NaN", { x: Number.NaN, y: 0.5 }],
    ["Infinity", { x: Number.POSITIVE_INFINITY, y: 0.5 }],
  ])("drops %s rather than drawing it at the origin", (_label, pointer) => {
    const rows = [presence({ state: { pointer } as CoworkingPresence["state"] })];
    expect(pointing(rows)).toEqual([]);
  });

  it("keeps the exact corners", () => {
    // 0 is falsy, which is the classic way a corner cursor disappears.
    const rows = [
      presence({ actor_id: "a", state: { pointer: { x: 0, y: 0 } } }),
      presence({ actor_id: "b", state: { pointer: { x: 1, y: 1 } } }),
    ];
    expect(pointing(rows)).toHaveLength(2);
  });
});
