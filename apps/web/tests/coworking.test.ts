import type { CoworkingPresence } from "@workspace/api-client";
import { describe, expect, it } from "vitest";
import {
  dedupeByActor,
  surfacePhrase,
} from "../components/coworking-strip";
import {
  isEditing,
  liveDraftOf,
  splitForCaret,
} from "../components/views/document-live";

/**
 * The live layer's pure core. Two claims carry the whole Google-Docs feel and
 * both are pinned here: a remote caret splits the local text exactly (with
 * out-of-range offsets clamped, because the remote draft can be a keystroke
 * newer than what this client holds), and "whose draft do I follow" always
 * has one answer — the newest editor carrying a draft — so two frames never
 * disagree about what the following pane shows.
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

describe("splitForCaret", () => {
  it("splits at a bare cursor", () => {
    expect(splitForCaret("hello world", 5)).toEqual({
      before: "hello",
      selected: "",
      after: " world",
    });
  });

  it("carries a selection as the middle slice", () => {
    expect(splitForCaret("hello world", undefined, 6, 11)).toEqual({
      before: "hello ",
      selected: "world",
      after: "",
    });
  });

  it("accepts a backwards selection", () => {
    expect(splitForCaret("hello world", undefined, 11, 6).selected).toBe("world");
  });

  it("clamps offsets beyond the local text to its end", () => {
    // The remote draft can be newer than the local text; the caret pins to
    // the end rather than vanishing or throwing.
    expect(splitForCaret("short", 99)).toEqual({
      before: "short",
      selected: "",
      after: "",
    });
  });

  it("pins to the end when there is no position at all", () => {
    expect(splitForCaret("text").before).toBe("text");
  });
});

describe("liveDraftOf / isEditing", () => {
  it("a typing presence is editing even before a draft arrives", () => {
    expect(isEditing(presence({ state: { typing: true } }))).toBe(true);
    expect(isEditing(presence({ state: {} }))).toBe(false);
  });

  it("follows the newest presence that carries a draft", () => {
    const older = presence({
      actor_id: "a",
      state: { draft: "old" },
      updated_at: "2026-08-25T10:00:00Z",
    });
    const newer = presence({
      actor_id: "b",
      state: { draft: "new" },
      updated_at: "2026-08-25T10:00:05Z",
    });
    const watcher = presence({ actor_id: "c", state: { cursor: 2 } });
    expect(liveDraftOf([older, watcher, newer])).toBe(newer);
  });

  it("follows nobody when nobody carries a draft", () => {
    expect(liveDraftOf([presence({ state: { typing: true } })])).toBeNull();
  });
});

describe("the strip's dedupe and phrasing", () => {
  it("keeps one chip per actor — their most recent surface", () => {
    const doc = presence({ updated_at: "2026-08-25T10:00:09Z" });
    const chat = presence({
      surface: "conversation:c1",
      updated_at: "2026-08-25T10:00:03Z",
    });
    const rows = dedupeByActor([chat, doc]);
    expect(rows).toHaveLength(1);
    expect(rows[0].surface).toBe("document:d1");
  });

  it("phrases a surface for a tooltip", () => {
    expect(surfacePhrase("document:d1")).toBe("in a document");
    expect(surfacePhrase("conversation:c1")).toBe("in a chat");
    expect(surfacePhrase("board:b1")).toBe("on a board");
    expect(surfacePhrase("somewhere-new")).toBe("here");
  });
});
