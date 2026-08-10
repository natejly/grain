import { describe, expect, it } from "vitest";
import { cardSlot, reindex } from "../components/views/board-columns";

/**
 * These three pure functions decide where a dragged card or column actually
 * lands, so they are the whole user-visible correctness of board reordering.
 * They shipped untested; the exhaustive checks below are the regression net.
 *
 * The shared subtlety: a drop `slot` is an index into the list *including* the
 * dragged item, but the item is removed before reinsertion. Dropping an item
 * after its own position must therefore shift down by one, or every downward
 * drag lands one place short.
 */

/** Reference implementation, deliberately naive — splice on a copy. */
function expectedOrder(order: string[], id: string, slot: number): string[] {
  const rest = order.filter((item) => item !== id);
  const from = order.indexOf(id);
  const to = from >= 0 && from < slot ? slot - 1 : slot;
  rest.splice(Math.max(0, Math.min(to, rest.length)), 0, id);
  return rest;
}

describe("reindex", () => {
  it("is a no-op when an item is dropped where it already is", () => {
    const order = ["a", "b", "c"];
    expect(reindex(order, "b", 1)).toEqual(order);
    expect(reindex(order, "b", 2)).toEqual(order);
  });

  it("moves an item to the front and to the back", () => {
    expect(reindex(["a", "b", "c"], "c", 0)).toEqual(["c", "a", "b"]);
    expect(reindex(["a", "b", "c"], "a", 3)).toEqual(["b", "c", "a"]);
  });

  it("clamps a slot past the end instead of dropping the item", () => {
    expect(reindex(["a", "b", "c"], "a", 99)).toEqual(["b", "c", "a"]);
  });

  it("clamps a negative slot to the front", () => {
    expect(reindex(["a", "b", "c"], "c", -5)).toEqual(["c", "a", "b"]);
  });

  it("handles a single-item list", () => {
    expect(reindex(["a"], "a", 0)).toEqual(["a"]);
    expect(reindex(["a"], "a", 1)).toEqual(["a"]);
  });

  it("leaves the list alone for an id it does not contain", () => {
    expect(reindex(["a", "b"], "zzz", 1)).toEqual(["a", "zzz", "b"]);
  });

  it("always returns a permutation, for every list size, item and slot", () => {
    for (let size = 1; size <= 6; size += 1) {
      const order = Array.from({ length: size }, (_, index) => `i${index}`);
      for (const id of order) {
        for (let slot = -1; slot <= size + 1; slot += 1) {
          const result = reindex(order, id, slot);
          expect([...result].sort()).toEqual([...order].sort());
          expect(result).toEqual(expectedOrder(order, id, slot));
        }
      }
    }
  });
});

describe("cardSlot", () => {
  it("clamps into range when moving between columns", () => {
    expect(cardSlot(["a", "b"], "x", 0, false)).toBe(0);
    expect(cardSlot(["a", "b"], "x", 2, false)).toBe(2);
    expect(cardSlot(["a", "b"], "x", 99, false)).toBe(2);
    expect(cardSlot(["a", "b"], "x", -3, false)).toBe(0);
  });

  it("discounts the dragged card when it stays in the column", () => {
    // Dragging "a" (index 0) below "c" means slot 3 in the including-self list,
    // which is index 2 once "a" is removed.
    expect(cardSlot(["a", "b", "c"], "a", 3, true)).toBe(2);
    // Dragging "c" up to the front does not shift: nothing before it is removed.
    expect(cardSlot(["a", "b", "c"], "c", 0, true)).toBe(0);
  });

  it("never returns an index outside the destination column", () => {
    for (let size = 1; size <= 6; size += 1) {
      const ids = Array.from({ length: size }, (_, index) => `c${index}`);
      for (const id of ids) {
        for (let slot = -1; slot <= size + 1; slot += 1) {
          const within = cardSlot(ids, id, slot, true);
          expect(within).toBeGreaterThanOrEqual(0);
          expect(within).toBeLessThanOrEqual(size - 1);

          const across = cardSlot(ids, "outsider", slot, false);
          expect(across).toBeGreaterThanOrEqual(0);
          expect(across).toBeLessThanOrEqual(size);
        }
      }
    }
  });

  it("agrees with reindex about where a same-column drag lands", () => {
    for (let size = 2; size <= 6; size += 1) {
      const ids = Array.from({ length: size }, (_, index) => `c${index}`);
      for (const id of ids) {
        for (let slot = 0; slot <= size; slot += 1) {
          // The server inserts at cardSlot's index; the optimistic client
          // computes the same list via reindex. They must not disagree, or the
          // board visibly jumps when the server response arrives.
          const index = cardSlot(ids, id, slot, true);
          const server = ids.filter((item) => item !== id);
          server.splice(index, 0, id);
          expect(server).toEqual(reindex(ids, id, slot));
        }
      }
    }
  });
});
