import { describe, expect, it } from "vitest";
import {
  addSnooze,
  nextMorning,
  parseSnoozes,
  pruneSnoozes,
  removeSnooze,
  snoozedIds,
} from "../components/views/snooze";

/**
 * The snooze module's whole contract is totality plus one piece of clock
 * arithmetic. The persisted map is a hand-editable localStorage key, so every
 * hostile shape must come back as an empty (or partially-salvaged) map rather
 * than a throw; the expiry boundary decides whether a row nags, so it is
 * pinned exactly; and nextMorning is asserted on wall-clock properties, not on
 * millisecond offsets, so the suite does not care which side of a DST change
 * it runs on.
 */
describe("parseSnoozes is total against hostile input", () => {
  it("treats missing and malformed values as an empty map", () => {
    expect(parseSnoozes(null)).toEqual({});
    expect(parseSnoozes("")).toEqual({});
    expect(parseSnoozes("not json")).toEqual({});
    expect(parseSnoozes('"a string"')).toEqual({});
    expect(parseSnoozes("42")).toEqual({});
    expect(parseSnoozes("[1,2,3]")).toEqual({});
    expect(parseSnoozes("null")).toEqual({});
  });

  it("drops entries that are not id → parseable date, keeping the rest", () => {
    const raw = JSON.stringify({
      good: "2026-08-24T09:00:00.000Z",
      numeric: 5,
      gibberish: "not a date",
      nested: { until: "2026-08-24T09:00:00.000Z" },
    });
    expect(parseSnoozes(raw)).toEqual({ good: "2026-08-24T09:00:00.000Z" });
  });
});

describe("snoozedIds filters by the wake time", () => {
  const now = new Date("2026-08-23T12:00:00.000Z");

  it("keeps a future snooze asleep and lets an expired one wake", () => {
    const asleep = snoozedIds(
      {
        future: "2026-08-24T09:00:00.000Z",
        past: "2026-08-22T09:00:00.000Z",
      },
      now,
    );
    expect(asleep.has("future")).toBe(true);
    expect(asleep.has("past")).toBe(false);
  });

  it("wakes exactly AT the wake time — the boundary is exclusive", () => {
    const atBoundary = snoozedIds({ id: now.toISOString() }, now);
    expect(atBoundary.has("id")).toBe(false);
    const justBefore = snoozedIds(
      { id: new Date(now.getTime() + 1).toISOString() },
      now,
    );
    expect(justBefore.has("id")).toBe(true);
  });
});

describe("addSnooze and removeSnooze", () => {
  it("adds without mutating the input", () => {
    const before = { a: "2026-08-24T09:00:00.000Z" };
    const until = new Date("2026-08-25T09:00:00.000Z");
    const after = addSnooze(before, "b", until);
    expect(after).toEqual({ a: "2026-08-24T09:00:00.000Z", b: until.toISOString() });
    expect(before).toEqual({ a: "2026-08-24T09:00:00.000Z" });
  });

  it("removes without mutating the input, and tolerates an absent id", () => {
    const before = { a: "2026-08-24T09:00:00.000Z" };
    expect(removeSnooze(before, "a")).toEqual({});
    expect(removeSnooze(before, "missing")).toEqual(before);
    expect(before).toEqual({ a: "2026-08-24T09:00:00.000Z" });
  });
});

describe("nextMorning", () => {
  it("is tomorrow at 09:00 local, even when pressed before nine", () => {
    // Local-time constructor on purpose: the contract is wall-clock local.
    const wake = nextMorning(new Date(2026, 7, 23, 8, 0, 0));
    expect(wake.getFullYear()).toBe(2026);
    expect(wake.getMonth()).toBe(7);
    expect(wake.getDate()).toBe(24);
    expect(wake.getHours()).toBe(9);
    expect(wake.getMinutes()).toBe(0);
  });

  it("crosses midnight and month ends by calendar, not by adding hours", () => {
    const wake = nextMorning(new Date(2026, 7, 31, 23, 59, 59));
    expect(wake.getMonth()).toBe(8);
    expect(wake.getDate()).toBe(1);
    expect(wake.getHours()).toBe(9);
  });

  it("lands on the wall-clock nine across a DST change", () => {
    // 2026-03-08 is the US spring-forward date; whatever zone runs the suite,
    // the assertion is on the local reading, which setHours guarantees.
    const wake = nextMorning(new Date(2026, 2, 7, 22, 0, 0));
    expect(wake.getDate()).toBe(8);
    expect(wake.getHours()).toBe(9);
    expect(wake.getTime()).toBeGreaterThan(new Date(2026, 2, 7, 22, 0, 0).getTime());
  });
});

describe("pruneSnoozes", () => {
  const now = new Date("2026-08-23T12:00:00Z");
  const future = "2026-08-24T09:00:00.000Z";

  it("drops entries the waiting set no longer holds", () => {
    const map = { kept: future, decided: future };
    const pruned = pruneSnoozes(map, new Set(["kept"]), now);
    expect(pruned).toEqual({ kept: future });
  });

  it("drops entries already woken, even for ids still waiting", () => {
    const map = { awake: "2026-08-23T09:00:00.000Z", asleep: future };
    const pruned = pruneSnoozes(map, new Set(["awake", "asleep"]), now);
    expect(pruned).toEqual({ asleep: future });
  });

  it("returns the identical object when nothing changed, so callers can skip the write", () => {
    const map = { kept: future };
    expect(pruneSnoozes(map, new Set(["kept"]), now)).toBe(map);
  });
});
