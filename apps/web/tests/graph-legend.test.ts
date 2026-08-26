import { describe, expect, it } from "vitest";
import { entityLegend } from "../components/graph-3d";

/**
 * The legend is derived from the palette rather than written beside it, so
 * what is worth asserting is the derivation: every entity type the palette
 * colours appears, in both themes, as a CSS-ready hex string under a label
 * fit for a human. The rendered swatches are just this list; the three.js
 * spheres read the same palette, so the two cannot drift apart.
 */
const TYPES = ["concept", "named_entity", "organization", "project"];

describe("entityLegend", () => {
  for (const theme of ["dark", "light"] as const) {
    it(`covers every palette type in ${theme}`, () => {
      const entries = entityLegend(theme);
      expect(entries.map((entry) => entry.type).sort()).toEqual(TYPES);
      for (const entry of entries) {
        expect(entry.color).toMatch(/^#[0-9a-f]{6}$/);
      }
    });
  }

  it("labels types as words, not schema identifiers", () => {
    const labels = new Map(
      entityLegend("dark").map((entry) => [entry.type, entry.label]),
    );
    expect(labels.get("named_entity")).toBe("entity");
    for (const label of labels.values()) {
      expect(label).not.toContain("_");
    }
  });

  it("gives each theme its own hues", () => {
    // Colours tuned for #0b0d10 wash out on cream; identical lists would mean
    // one theme is borrowing the other's palette.
    expect(entityLegend("dark").map((entry) => entry.color)).not.toEqual(
      entityLegend("light").map((entry) => entry.color),
    );
  });
});
