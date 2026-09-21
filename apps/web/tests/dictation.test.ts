import { describe, expect, it } from "vitest";
import {
  applyResult,
  beginDictation,
  composedDraft,
  resultWrite,
  supportsDictation,
} from "../components/views/dictation";

/**
 * The reducer IS the dictation feature's tested truth: the mic button in
 * chat.tsx is a thin adapter, because the Web Speech API cannot be exercised
 * in CI (headless Chrome exposes the constructor and then fails recognition
 * with a network error at runtime).
 */
describe("supportsDictation", () => {
  it("is false for a bare object and for no window at all", () => {
    expect(supportsDictation({})).toBe(false);
    expect(supportsDictation(null)).toBe(false);
    expect(supportsDictation(undefined)).toBe(false);
  });

  it("is true for a window carrying either constructor", () => {
    expect(supportsDictation({ webkitSpeechRecognition: function fake() {} })).toBe(
      true,
    );
    expect(supportsDictation({ SpeechRecognition: function fake() {} })).toBe(true);
  });
});

describe("the dictation reducer", () => {
  it("accumulates finals across result events", () => {
    let state = beginDictation("");
    state = applyResult(state, [{ transcript: "hello", isFinal: true }]);
    state = applyResult(state, [{ transcript: "world", isFinal: true }]);
    expect(state.committed).toBe("hello world");
    expect(composedDraft(state)).toBe("hello world");
  });

  it("replaces the interim wholesale rather than appending it", () => {
    let state = beginDictation("");
    state = applyResult(state, [{ transcript: "hel", isFinal: false }]);
    expect(state.interim).toBe("hel");
    state = applyResult(state, [{ transcript: "hello th", isFinal: false }]);
    // The hypothesis was revised, not extended: "hel" must be gone.
    expect(state.interim).toBe("hello th");
    expect(composedDraft(state)).toBe("hello th");
  });

  it("promotes a final and keeps a following interim in one event", () => {
    let state = beginDictation("");
    state = applyResult(state, [
      { transcript: "first sentence.", isFinal: true },
      { transcript: "second sen", isFinal: false },
    ]);
    expect(composedDraft(state)).toBe("first sentence. second sen");
    // The next event revises only the interim (the resultIndex contract:
    // the caller hands over the slice from event.resultIndex).
    state = applyResult(state, [{ transcript: "second sentence.", isFinal: true }]);
    expect(composedDraft(state)).toBe("first sentence. second sentence.");
  });

  it("composes over an empty base without inventing a space", () => {
    let state = beginDictation("");
    state = applyResult(state, [{ transcript: "hi", isFinal: true }]);
    expect(composedDraft(state)).toBe("hi");
  });

  it("adds nothing after a base that already ends in whitespace", () => {
    let state = beginDictation("draft ");
    state = applyResult(state, [{ transcript: "more", isFinal: true }]);
    expect(composedDraft(state)).toBe("draft more");
  });

  it("separates a mid-word base with exactly one space", () => {
    let state = beginDictation("draft");
    state = applyResult(state, [{ transcript: "more", isFinal: true }]);
    expect(composedDraft(state)).toBe("draft more");
  });

  it("leaves the base untouched while nothing has been spoken", () => {
    const state = beginDictation("untouched");
    expect(composedDraft(state)).toBe("untouched");
  });

  it("lets a result overwrite only the draft dictation itself last wrote", () => {
    // The undisturbed case: the draft still reads as the last composed
    // write, so the new speech replaces it.
    expect(resultWrite("hello", "hello", "hello world")).toBe("hello world");
  });

  it("keeps typed text when a result lands after a racing keystroke", () => {
    // The race the stop-on-edit effect cannot catch: the keystroke's write
    // ("hello!") is still queued when a result event fires. The functional
    // draft the updater sees differs from the last composed write, so the
    // person's text stands and the composed value — built from the frozen
    // base, containing none of the typing — is discarded.
    expect(resultWrite("hello!", "hello", "hello world")).toBe("hello!");
    // Including a keystroke that DELETED part of the spoken text.
    expect(resultWrite("hell", "hello", "hello world")).toBe("hell");
  });

  it("begins a fresh base from the current draft on stop/restart", () => {
    let first = beginDictation("start");
    first = applyResult(first, [{ transcript: "spoken once", isFinal: true }]);
    const draftAfterStop = composedDraft(first);
    expect(draftAfterStop).toBe("start spoken once");
    // The person edits, then records again: the new session's base is the
    // edited draft, and the old session's speech is not replayed into it.
    const edited = `${draftAfterStop} plus typing`;
    let second = beginDictation(edited);
    expect(second.committed).toBe("");
    second = applyResult(second, [{ transcript: "spoken twice", isFinal: true }]);
    expect(composedDraft(second)).toBe("start spoken once plus typing spoken twice");
  });
});
