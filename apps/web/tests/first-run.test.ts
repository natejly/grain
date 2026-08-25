import { afterEach, describe, expect, it } from "vitest";
import {
  FIRST_RUN_KEY,
  hasSeen,
  markSeen,
  parseSeen,
  serializeSeen,
} from "../components/views/first-run";

/**
 * The seen-mark set behind "shown once, ever". The persisted value is a
 * hand-editable localStorage key, so the parser is pinned as total — any
 * hostile value is an empty set, never a throw that takes a transcript down
 * over a teaching caption — and the storage-backed pair is exercised against
 * jsdom's real localStorage, the same surface the views reach.
 */

afterEach(() => window.localStorage.clear());

describe("parseSeen is total", () => {
  it("reads nothing from a missing or malformed value", () => {
    expect(parseSeen(null).size).toBe(0);
    expect(parseSeen("").size).toBe(0);
    expect(parseSeen("not json").size).toBe(0);
    expect(parseSeen('"a string"').size).toBe(0);
    expect(parseSeen('{"approval-loop": true}').size).toBe(0);
    expect(parseSeen("42").size).toBe(0);
  });

  it("drops non-string entries one at a time rather than the whole set", () => {
    const seen = parseSeen('["approval-loop", 7, null, {"x": 1}, "other"]');
    expect([...seen].sort()).toEqual(["approval-loop", "other"]);
  });

  it("roundtrips through serializeSeen", () => {
    const seen = new Set(["approval-loop", "another-moment"]);
    expect(parseSeen(serializeSeen(seen))).toEqual(seen);
  });
});

describe("the storage-backed pair", () => {
  it("has seen nothing until something is marked", () => {
    expect(hasSeen("approval-loop")).toBe(false);
    markSeen("approval-loop");
    expect(hasSeen("approval-loop")).toBe(true);
    // An unknown id stays unseen — marks are per moment, not a global flag.
    expect(hasSeen("some-other-moment")).toBe(false);
  });

  it("accumulates marks instead of replacing them", () => {
    markSeen("approval-loop");
    markSeen("another-moment");
    expect(hasSeen("approval-loop")).toBe(true);
    expect(hasSeen("another-moment")).toBe(true);
  });

  it("marks over a hostile stored value rather than throwing", () => {
    window.localStorage.setItem(FIRST_RUN_KEY, '{"poisoned"');
    expect(hasSeen("approval-loop")).toBe(false);
    markSeen("approval-loop");
    expect(hasSeen("approval-loop")).toBe(true);
  });
});
