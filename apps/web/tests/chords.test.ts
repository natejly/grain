// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import {
  CHORD_VIEWS,
  chordEligible,
  chordHint,
  chordTarget,
  isTypingContext,
  parseChordsEnabled,
  serializeChordsEnabled,
} from "../components/views/chords";

describe("chordTarget", () => {
  it("maps the five letters to their destinations", () => {
    expect(chordTarget("c")).toBe("chat");
    expect(chordTarget("i")).toBe("activity");
    expect(chordTarget("l")).toBe("documents");
    expect(chordTarget("a")).toBe("workflows");
    expect(chordTarget("d")).toBe("dashboards");
  });

  it("is case-insensitive and refuses everything else", () => {
    expect(chordTarget("C")).toBe("chat");
    expect(chordTarget("x")).toBeNull();
    expect(chordTarget("Enter")).toBeNull();
  });
});

describe("chordHint", () => {
  it("names the chord for a destination that has one", () => {
    expect(chordHint("chat")).toBe("G C");
    expect(chordHint("dashboards")).toBe("G D");
  });

  it("stays quiet for views without a chord", () => {
    expect(chordHint("memory")).toBeNull();
  });

  it("agrees with the chord table both ways", () => {
    for (const chord of CHORD_VIEWS) {
      expect(chordTarget(chord.key)).toBe(chord.view);
      expect(chordHint(chord.view)).toBe(`G ${chord.key.toUpperCase()}`);
    }
  });
});

describe("chordEligible", () => {
  const bare = { key: "g", metaKey: false, ctrlKey: false, altKey: false };

  it("accepts a bare letter aimed at the page", () => {
    expect(chordEligible(true, { ...bare, target: document.body })).toBe(true);
    expect(chordEligible(true, { ...bare, target: null })).toBe(true);
  });

  it("refuses everything while the kill-switch is off", () => {
    // The switch is the FIRST argument on purpose: a listener cannot consult
    // the key rules without also consulting it.
    expect(chordEligible(false, { ...bare, target: document.body })).toBe(false);
    expect(chordEligible(false, { ...bare, target: null })).toBe(false);
  });

  it("refuses modified keys — ⌘G and friends belong to the browser", () => {
    expect(chordEligible(true, { ...bare, metaKey: true, target: document.body })).toBe(false);
    expect(chordEligible(true, { ...bare, ctrlKey: true, target: document.body })).toBe(false);
    expect(chordEligible(true, { ...bare, altKey: true, target: document.body })).toBe(false);
  });

  it("refuses non-letter keys", () => {
    expect(chordEligible(true, { ...bare, key: "Escape", target: document.body })).toBe(false);
  });

  it("refuses keys typed into a field — chords must not fire mid-sentence", () => {
    for (const tag of ["input", "textarea", "select"] as const) {
      expect(chordEligible(true, { ...bare, target: document.createElement(tag) })).toBe(false);
    }
    const editable = document.createElement("div");
    // jsdom does not compute isContentEditable from the attribute alone.
    Object.defineProperty(editable, "isContentEditable", { value: true });
    expect(chordEligible(true, { ...bare, target: editable })).toBe(false);
    expect(chordEligible(true, { ...bare, target: document.createElement("div") })).toBe(true);
  });
});

describe("the '?' cheat-sheet key composes with the same guards", () => {
  // The shell's "?" listener reuses isTypingContext directly — these pin the
  // predicate's answers for the shapes that listener sees.
  it("isTypingContext suppresses '?' typed into a field", () => {
    for (const tag of ["input", "textarea"] as const) {
      expect(isTypingContext(document.createElement(tag))).toBe(true);
    }
    const editable = document.createElement("div");
    // jsdom does not compute isContentEditable from the attribute alone.
    Object.defineProperty(editable, "isContentEditable", { value: true });
    expect(isTypingContext(editable)).toBe(true);
  });

  it("isTypingContext lets '?' through from the page", () => {
    // Falsy, not `false`: jsdom leaves isContentEditable undefined on a plain
    // element (a real browser says false), and the listener only ever negates.
    expect(isTypingContext(document.body)).toBeFalsy();
    expect(isTypingContext(null)).toBeFalsy();
    expect(isTypingContext(document.createElement("div"))).toBeFalsy();
  });

  it("chordEligible rejects nothing new — shift is not a disqualifier", () => {
    // '?' is shift+/ on most layouts, and chordEligible never consults
    // shiftKey: a single character with no meta/ctrl/alt stays eligible.
    const question = { key: "?", metaKey: false, ctrlKey: false, altKey: false };
    expect(chordEligible(true, { ...question, target: document.body })).toBe(true);
    expect(
      chordEligible(true, { ...question, target: document.createElement("input") }),
    ).toBe(false);
  });
});

describe("the kill-switch encoding", () => {
  it('is on for anything except the literal "off" — missing and hostile included', () => {
    expect(parseChordsEnabled(null)).toBe(true);
    expect(parseChordsEnabled("")).toBe(true);
    expect(parseChordsEnabled("banana")).toBe(true);
    expect(parseChordsEnabled("off")).toBe(false);
  });

  it("round-trips through its serializer", () => {
    expect(parseChordsEnabled(serializeChordsEnabled(false))).toBe(false);
    expect(parseChordsEnabled(serializeChordsEnabled(true))).toBe(true);
  });
});
