import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  Conversation,
  GraphEntity,
  KnowledgeGraph,
  MemoryItem,
  Source,
  Space,
} from "@workspace/api-client";
import { GraphView } from "../components/views/graph";
import { MemoryView } from "../components/views/memory";
import { SourcesView } from "../components/views/sources";

// The canvas is three.js behind a dynamic import; jsdom has no WebGL and the
// links under test live in the entity list beside it, not in it.
vi.mock("../components/graph-3d", () => ({ Graph3D: () => null }));

/**
 * The three Knowledge views point into each other.
 *
 * Sources, Memory, and Graph are one system — files become passages, passages
 * and conversation become memories and entities, entities project both — but
 * each page rendered its neighbours' ids as inert text. These tests pin the
 * links in every direction, and the two provenance facts that make them
 * honest: a memory names the thread that taught it (or says why it cannot),
 * and a scoped row wears its space.
 */
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
    owned: true,
    can_share: true,
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:00Z",
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

function source(overrides: Partial<Source> = {}): Source {
  return {
    id: "src-1",
    filename: "notes.md",
    media_type: "text/markdown",
    byte_size: 10,
    status: "ready",
    error: "",
    chunk_count: 3,
    space_id: "",
    created_at: "2026-08-01T00:00:00Z",
    ...overrides,
  };
}

function memory(overrides: Partial<MemoryItem> = {}): MemoryItem {
  return {
    id: "m1",
    conversation_id: null,
    space_id: "",
    kind: "fact",
    content: "The API deploys on Railway.",
    entity_names: [],
    message_ids: [],
    importance: 1,
    shared: true,
    created_at: "2026-08-01T00:00:00",
    updated_at: "2026-08-01T00:00:00",
    ...overrides,
  };
}

function entity(overrides: Partial<GraphEntity> = {}): GraphEntity {
  return {
    id: "e1",
    name: "Railway",
    entity_type: "technology",
    mention_count: 2,
    source_ids: [],
    chunk_ids: [],
    memory_ids: [],
    ...overrides,
  };
}

function graphOf(entities: GraphEntity[]): KnowledgeGraph {
  // "ready" so the stale-repair effect stays quiet; it has its own suite.
  return { status: "ready", version: "v1", built_at: "2026-08-01T00:00:00", entities, edges: [] };
}

function sourcesView(overrides: Partial<Parameters<typeof SourcesView>[0]> = {}) {
  return createElement(SourcesView, {
    sources: [source()],
    setError: () => undefined,
    uploading: false,
    dragging: false,
    setDragging: () => undefined,
    uploadFiles: async () => undefined,
    removeSource: async () => undefined,
    fileInputRef: { current: null },
    ...overrides,
  });
}

function graphView(overrides: Partial<Parameters<typeof GraphView>[0]> = {}) {
  return createElement(GraphView, {
    graph: graphOf([entity()]),
    rebuild: async () => undefined,
    openChunk: async () => undefined,
    ...overrides,
  });
}

afterEach(cleanup);

describe("Sources → Graph", () => {
  it("counts the entities a file projected and lands on the first of them", () => {
    const openEntity = vi.fn();
    render(
      sourcesView({
        graph: graphOf([
          entity({ id: "e1", source_ids: ["src-1"] }),
          entity({ id: "e2", source_ids: ["other"] }),
        ]),
        openEntity,
      }),
    );
    const link = screen.getByRole("button", { name: "3 passages · 1 entity" });
    fireEvent.click(link);
    expect(openEntity).toHaveBeenCalledWith("e1");
  });

  it("stays a plain count for a file the graph has not read", () => {
    render(sourcesView({ graph: graphOf([entity({ source_ids: ["other"] })]), openEntity: vi.fn() }));
    expect(screen.queryByRole("button", { name: /passages/ })).toBeNull();
    expect(screen.getByText("3")).toBeTruthy();
  });

  it("names a scoped file's space on the row", () => {
    const { container } = render(
      sourcesView({
        sources: [source({ space_id: "space-1" })],
        spaces: [space()],
      }),
    );
    const chip = container.querySelector(".source-space");
    expect(chip?.textContent).toBe("Research");
  });
});

describe("Memory → Chat", () => {
  it("links a memory to the thread that taught it", () => {
    const openConversation = vi.fn();
    render(
      createElement(MemoryView, {
        memories: [memory({ conversation_id: "conv-1" })],
        forgetMemory: async () => undefined,
        conversations: [conversation({ id: "conv-1", title: "Deploy planning" })],
        openConversation,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "from Deploy planning" }));
    expect(openConversation).toHaveBeenCalledWith("conv-1");
  });

  it("says why a thread cannot be opened instead of pointing at nothing", () => {
    // Someone else's personal thread and a deleted one are the same fact from
    // here: the list does not hold it.
    render(
      createElement(MemoryView, {
        memories: [memory({ conversation_id: "conv-gone" })],
        forgetMemory: async () => undefined,
        conversations: [conversation()],
      }),
    );
    expect(
      screen.getByText(/learned in a thread you can't see or that was deleted/),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^from / })).toBeNull();
  });
});

describe("Memory → Graph", () => {
  it("links an entity name to its graph row, provenance first", () => {
    const openEntity = vi.fn();
    render(
      createElement(MemoryView, {
        memories: [memory({ entity_names: ["Railway"] })],
        forgetMemory: async () => undefined,
        graph: graphOf([
          entity({ id: "by-name", name: "Railway" }),
          entity({ id: "by-memory", name: "Railway", memory_ids: ["m1"] }),
        ]),
        openEntity,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Railway" }));
    expect(openEntity).toHaveBeenCalledWith("by-memory");
  });

  it("leaves a name the graph does not know as text", () => {
    render(
      createElement(MemoryView, {
        memories: [memory({ entity_names: ["Railway"], content: "Deploys fine." })],
        forgetMemory: async () => undefined,
        graph: graphOf([]),
        openEntity: vi.fn(),
      }),
    );
    expect(screen.queryByRole("button", { name: "Railway" })).toBeNull();
    expect(screen.getByText(/Railway/)).toBeTruthy();
  });
});

describe("the memory space chip", () => {
  it("wears the space on the row without touching the scope badge", () => {
    const { container } = render(
      createElement(MemoryView, {
        memories: [memory({ space_id: "space-1" })],
        forgetMemory: async () => undefined,
        spaces: [space()],
      }),
    );
    expect(container.querySelector(".memory-space")?.textContent).toBe("Research");
    expect(container.querySelectorAll(".memory-scope").length).toBe(1);
  });

  it("makes the space searchable, name and the word both", () => {
    render(
      createElement(MemoryView, {
        memories: [
          memory({ id: "spaced", space_id: "space-1", content: "Alpha" }),
          memory({ id: "bare", content: "Beta" }),
        ],
        forgetMemory: async () => undefined,
        spaces: [space()],
      }),
    );
    fireEvent.change(screen.getByLabelText("Search memory"), {
      target: { value: "research space" },
    });
    expect(screen.getByText("Alpha")).toBeTruthy();
    expect(screen.queryByText("Beta")).toBeNull();
  });
});

describe("Graph → Sources", () => {
  it("names the first source that mentions an entity and lands on its row", () => {
    const openSource = vi.fn();
    render(
      graphView({
        graph: graphOf([entity({ source_ids: ["src-1", "src-2"] })]),
        sources: [source(), source({ id: "src-2", filename: "spec.pdf" })],
        openSource,
      }),
    );
    const chip = screen.getByRole("button", { name: "notes.md +1" });
    fireEvent.click(chip);
    expect(openSource).toHaveBeenCalledWith("src-1");
  });

  it("shows no chip when every source id is stale", () => {
    render(
      graphView({
        graph: graphOf([entity({ source_ids: ["deleted"] })]),
        sources: [source()],
        openSource: vi.fn(),
      }),
    );
    expect(screen.queryByRole("button", { name: /notes\.md/ })).toBeNull();
  });
});

describe("Graph → Memory", () => {
  it("turns 'from memory' into the count and lands on the first memory", () => {
    const openMemory = vi.fn();
    render(
      graphView({
        graph: graphOf([entity({ memory_ids: ["m1", "m2"] })]),
        openMemory,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "from 2 memories" }));
    expect(openMemory).toHaveBeenCalledWith("m1");
  });

  it("stays inert text when mounted without the link — the old suite's shape", () => {
    render(graphView({ graph: graphOf([entity({ memory_ids: ["m1"] })]) }));
    expect(screen.queryByRole("button", { name: /memor/ })).toBeNull();
    expect(screen.getByText(/from memory/)).toBeTruthy();
  });
});
