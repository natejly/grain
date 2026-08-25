import { describe, expect, it } from "vitest";
import {
  SPLIT_SIZES_KEY,
  applyDelta,
  equalSizes,
  parseStoredSizes,
  serializeStoredSizes,
} from "../components/views/split-sizes";

/**
 * The split's resize rules, tested without a DOM. Everything that decides how
 * wide a pane is — the drag arithmetic, the minimum that keeps a pane from
 * vanishing behind its own divider, and the per-column-count round trip
 * through localStorage — lives in these pure helpers; `ChatSplit` only wires
 * them to pointer events and React state.
 */

const MIN = 15;

describe("equal shares", () => {
  it("splits 100 evenly across the columns", () => {
    expect(equalSizes(2)).toEqual([50, 50]);
    expect(equalSizes(4)).toEqual([25, 25, 25, 25]);
  });
});

describe("moving a divider", () => {
  it("gives the delta to the left pane and takes it from the right", () => {
    expect(applyDelta([50, 50], 0, 10, MIN)).toEqual([60, 40]);
    expect(applyDelta([50, 50], 0, -10, MIN)).toEqual([40, 60]);
  });

  it("moves only the divider's two neighbours in a wider split", () => {
    expect(applyDelta([40, 30, 30], 1, 5, MIN)).toEqual([40, 35, 25]);
  });

  it("refuses the move that would crush the right pane below the minimum", () => {
    const sizes = [50, 50];
    // 50 - 40 = 10 < MIN: identity, so a state setter can skip the re-render.
    expect(applyDelta(sizes, 0, 40, MIN)).toBe(sizes);
  });

  it("refuses the move that would crush the left pane below the minimum", () => {
    const sizes = [20, 80];
    expect(applyDelta(sizes, 0, -10, MIN)).toBe(sizes);
  });

  it("admits the move that lands exactly on the minimum", () => {
    expect(applyDelta([50, 50], 0, 35, MIN)).toEqual([85, 15]);
  });

  it("does not mutate the array the caller handed in", () => {
    const sizes = [50, 50];
    applyDelta(sizes, 0, 10, MIN);
    expect(sizes).toEqual([50, 50]);
  });
});

describe("persisting the ratios", () => {
  it("uses the grain-namespaced storage key", () => {
    expect(SPLIT_SIZES_KEY).toBe("grain.split-sizes");
  });

  it("round-trips a drag through serialize and parse", () => {
    const raw = serializeStoredSizes(null, 2, [60, 40]);
    expect(parseStoredSizes(raw, 2)).toEqual([60, 40]);
  });

  it("keys the store per column count, so counts do not clobber each other", () => {
    // The reason the store is a map: opening a third pane and closing it again
    // must land back on the drag the user made at two.
    let raw = serializeStoredSizes(null, 2, [60, 40]);
    raw = serializeStoredSizes(raw, 3, [40, 30, 30]);
    expect(parseStoredSizes(raw, 2)).toEqual([60, 40]);
    expect(parseStoredSizes(raw, 3)).toEqual([40, 30, 30]);
  });

  it("overwrites the same count's earlier entry", () => {
    let raw = serializeStoredSizes(null, 2, [60, 40]);
    raw = serializeStoredSizes(raw, 2, [30, 70]);
    expect(parseStoredSizes(raw, 2)).toEqual([30, 70]);
  });

  it("starts over on a malformed prior store rather than throwing", () => {
    const raw = serializeStoredSizes("{not json", 2, [60, 40]);
    expect(parseStoredSizes(raw, 2)).toEqual([60, 40]);
  });

  it("reads a missing key as an even split", () => {
    expect(parseStoredSizes(null, 3)).toEqual(equalSizes(3));
  });

  it("survives malformed JSON rather than throwing", () => {
    // The value is one hand-editable localStorage key; a parse crash here
    // would take the whole split down on load.
    expect(parseStoredSizes("{not json", 2)).toEqual([50, 50]);
  });

  it("ignores a stored value that is not an object map", () => {
    expect(parseStoredSizes("[60,40]", 2)).toEqual([50, 50]);
    expect(parseStoredSizes('"60/40"', 2)).toEqual([50, 50]);
  });

  it("falls back when the count has no entry", () => {
    const raw = serializeStoredSizes(null, 2, [60, 40]);
    expect(parseStoredSizes(raw, 4)).toEqual(equalSizes(4));
  });

  it("rejects an entry of the wrong length", () => {
    expect(parseStoredSizes('{"2":[60,20,20]}', 2)).toEqual([50, 50]);
  });

  it("rejects non-numeric, non-finite, and non-positive entries", () => {
    expect(parseStoredSizes('{"2":["60",40]}', 2)).toEqual([50, 50]);
    expect(parseStoredSizes('{"2":[null,100]}', 2)).toEqual([50, 50]);
    expect(parseStoredSizes('{"2":[1e999,1]}', 2)).toEqual([50, 50]);
    expect(parseStoredSizes('{"2":[-10,110]}', 2)).toEqual([50, 50]);
  });

  it("rejects a hand-edited sum far from 100", () => {
    // flex-grow would still render *something*, but the aria values and the
    // minimum-width guard would all be lying about percentages.
    expect(parseStoredSizes('{"2":[10,10]}', 2)).toEqual([50, 50]);
    expect(parseStoredSizes('{"2":[300,300]}', 2)).toEqual([50, 50]);
  });

  it("tolerates float drift around 100", () => {
    const thirds = equalSizes(3);
    const raw = serializeStoredSizes(null, 3, thirds);
    expect(parseStoredSizes(raw, 3)).toEqual(thirds);
  });
});
