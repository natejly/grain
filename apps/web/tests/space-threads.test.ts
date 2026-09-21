import { describe, expect, it } from "vitest";
import type {
  Conversation,
  ProjectSummary,
  Source,
  Space,
} from "@workspace/api-client";
import {
  projectThreadGroups,
  sourcesInSpace,
  spaceNameForId,
  spaceNameOf,
  spaceThreadGroups,
  threadsInSpace,
  unfiledThreads,
  unspacedThreads,
} from "../components/views/space-threads";

function conversation(overrides: Partial<Conversation> = {}): Conversation {
  return {
    id: "conv-1",
    title: "Thread",
    subject_kind: "",
    subject_id: "",
    space_id: "",
    default_agent_id: "",
    default_model: "",
    default_effort: "",
    approval_mode: "ask_writes",
    shared: false,
    incognito: false,
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
    conversation_id: "",
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
    default_agent_id: "",
    thread_count: 0,
    source_count: 0,
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:00Z",
    ...overrides,
  };
}

function project(overrides: Partial<ProjectSummary> = {}): ProjectSummary {
  return {
    id: "project-1",
    name: "Kestrel",
    description: "",
    kind: "web",
    entry_path: "App.tsx",
    file_count: 1,
    total_bytes: 10,
    updated_at: "2026-08-01T00:00:00Z",
    ...overrides,
  };
}

/** A thread hanging off a project, the way the server returns one. */
function projectThread(projectId: string, id: string): Conversation {
  return conversation({ id, subject_kind: "project", subject_id: projectId });
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

describe("spaceThreadGroups / unspacedThreads", () => {
  const spaces = [
    space({ id: "space-1", name: "Research" }),
    space({ id: "space-2", name: "Empty" }),
  ];
  const rows = [
    conversation({ id: "a", space_id: "space-1" }),
    conversation({ id: "b" }),
    conversation({ id: "c", space_id: "space-1" }),
    conversation({ id: "orphan", space_id: "deleted-space" }),
  ];

  it("groups threads under their space, keeping both orders", () => {
    const groups = spaceThreadGroups(spaces, rows);
    expect(groups.map((group) => group.space.id)).toEqual(["space-1"]);
    expect(groups[0].threads.map((row) => row.id)).toEqual(["a", "c"]);
  });

  it("renders no group for a space with no visible threads", () => {
    expect(
      spaceThreadGroups(spaces, rows).some((group) => group.space.id === "space-2"),
    ).toBe(false);
  });

  it("partitions: whatever is not in a group is unspaced, so no row can vanish", () => {
    const grouped = spaceThreadGroups(spaces, rows).flatMap((group) =>
      group.threads.map((row) => row.id),
    );
    const flat = unspacedThreads(spaces, rows).map((row) => row.id);
    expect([...grouped, ...flat].sort()).toEqual(["a", "b", "c", "orphan"].sort());
    // The thread pointing at a deleted space falls back to the flat rail.
    expect(flat).toContain("orphan");
  });
});

describe("projectThreadGroups / unfiledThreads", () => {
  const spaces = [space({ id: "space-1", name: "Research" })];
  const projects = [
    project({ id: "project-1", name: "Kestrel" }),
    project({ id: "project-2", name: "Empty" }),
  ];
  const rows = [
    projectThread("project-1", "p1-a"),
    conversation({ id: "loose" }),
    conversation({ id: "spaced", space_id: "space-1" }),
    projectThread("project-1", "p1-b"),
    projectThread("gone", "orphan"),
  ];

  it("groups threads under their project, keeping both orders", () => {
    const groups = projectThreadGroups(projects, rows);
    expect(groups.map((group) => group.project.id)).toEqual(["project-1"]);
    expect(groups[0].threads.map((row) => row.id)).toEqual(["p1-a", "p1-b"]);
  });

  it("renders no group for a project with no threads", () => {
    expect(
      projectThreadGroups(projects, rows).some(
        (group) => group.project.id === "project-2",
      ),
    ).toBe(false);
  });

  it("never groups on the id alone — the kind has to match too", () => {
    // Three subject tables mint independent uuids, so a dashboard thread can
    // hold a project's id. Grouping on `subject_id` alone would file it here.
    const impostor = conversation({
      id: "dash",
      subject_kind: "dashboard",
      subject_id: "project-1",
    });
    expect(projectThreadGroups(projects, [impostor])).toEqual([]);
    expect(unfiledThreads([], projects, [impostor]).map((row) => row.id)).toEqual([
      "dash",
    ]);
  });

  it('never groups on "": no project swallows the ordinary rail', () => {
    const plain = [conversation({ id: "a" }), conversation({ id: "b" })];
    expect(projectThreadGroups([project({ id: "" })], plain)).toEqual([]);
    // And a project-kind row with no id stays in the flat rail rather than
    // joining the first group.
    const blank = conversation({ id: "blank", subject_kind: "project" });
    expect(projectThreadGroups(projects, [blank])).toEqual([]);
    expect(unfiledThreads([], projects, [blank]).map((row) => row.id)).toEqual([
      "blank",
    ]);
  });

  it("partitions three ways, so no row can vanish from the rail", () => {
    const spaced = spaceThreadGroups(spaces, rows).flatMap((group) =>
      group.threads.map((row) => row.id),
    );
    const grouped = projectThreadGroups(projects, rows).flatMap((group) =>
      group.threads.map((row) => row.id),
    );
    const flat = unfiledThreads(spaces, projects, rows).map((row) => row.id);
    expect([...spaced, ...grouped, ...flat].sort()).toEqual(
      rows.map((row) => row.id).sort(),
    );
    // The thread whose project is gone (or not yet fetched) falls through to
    // the flat rail rather than disappearing with its header.
    expect(flat).toContain("orphan");
    expect(flat).toContain("loose");
    expect(flat).not.toContain("p1-a");
    // A spaced thread is still the space group's, not the flat rail's.
    expect(spaced).toContain("spaced");
    expect(flat).not.toContain("spaced");
  });

  it("falls the whole way back while the projects list is still empty", () => {
    expect(projectThreadGroups([], rows)).toEqual([]);
    expect(unfiledThreads(spaces, [], rows).map((row) => row.id)).toEqual([
      "p1-a",
      "loose",
      "p1-b",
      "orphan",
    ]);
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

describe("spaceNameForId", () => {
  const spaces = [space({ id: "space-1", name: "Research" })];

  it("names the space for a bare id — source and memory rows hold no Conversation", () => {
    expect(spaceNameForId("space-1", spaces)).toBe("Research");
  });

  it('degrades to "" the same way: unspaced, deleted, or not yet loaded', () => {
    expect(spaceNameForId("", spaces)).toBe("");
    expect(spaceNameForId("deleted", spaces)).toBe("");
    expect(spaceNameForId("space-1", [])).toBe("");
  });
});
