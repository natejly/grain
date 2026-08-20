import { describe, expect, it } from "vitest";
import type { Conversation, Source, Space } from "@workspace/api-client";
import {
  sourcesInSpace,
  spaceNameOf,
  threadsInSpace,
} from "../components/views/space-threads";

function conversation(overrides: Partial<Conversation> = {}): Conversation {
  return {
    id: "conv-1",
    title: "Thread",
    subject_kind: "",
    subject_id: "",
    space_id: "",
    approval_mode: "ask_writes",
    shared: false,
    owned: true,
    can_share: true,
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:00Z",
    ...overrides,
  };
}

function source(overrides: Partial<Source> = {}): Source {
  return {
    id: "src-1",
    filename: "notes.md",
    media_type: "text/markdown",
    byte_size: 10,
    status: "ready",
    error: "",
    chunk_count: 1,
    space_id: "",
    created_at: "2026-08-01T00:00:00Z",
    ...overrides,
  };
}

function space(overrides: Partial<Space> = {}): Space {
  return {
    id: "space-1",
    name: "Research",
    instructions: "",
    thread_count: 0,
    source_count: 0,
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:00Z",
    ...overrides,
  };
}

describe("threadsInSpace", () => {
  it("keeps only the space's threads, in the order given", () => {
    const rows = [
      conversation({ id: "a", space_id: "space-1" }),
      conversation({ id: "b" }),
      conversation({ id: "c", space_id: "space-2" }),
      conversation({ id: "d", space_id: "space-1" }),
    ];
    expect(threadsInSpace(rows, "space-1").map((row) => row.id)).toEqual([
      "a",
      "d",
    ]);
  });

  it('matches nothing for "", never "every unspaced thread"', () => {
    const rows = [conversation({ id: "a" }), conversation({ id: "b" })];
    expect(threadsInSpace(rows, "")).toEqual([]);
  });
});

describe("sourcesInSpace", () => {
  it("keeps only the space's files", () => {
    const rows = [
      source({ id: "s1", space_id: "space-1" }),
      source({ id: "s2" }),
    ];
    expect(sourcesInSpace(rows, "space-1").map((row) => row.id)).toEqual(["s1"]);
  });

  it('matches nothing for ""', () => {
    expect(sourcesInSpace([source()], "")).toEqual([]);
  });
});

describe("spaceNameOf", () => {
  const spaces = [space({ id: "space-1", name: "Research" })];

  it("names the thread's space for the rail chip", () => {
    expect(spaceNameOf(conversation({ space_id: "space-1" }), spaces)).toBe(
      "Research",
    );
  });

  it("is empty for an unspaced thread", () => {
    expect(spaceNameOf(conversation(), spaces)).toBe("");
  });

  it("is empty — not a crash — when the space is gone or not yet loaded", () => {
    expect(spaceNameOf(conversation({ space_id: "deleted" }), spaces)).toBe("");
    expect(spaceNameOf(conversation({ space_id: "space-1" }), [])).toBe("");
  });
});
