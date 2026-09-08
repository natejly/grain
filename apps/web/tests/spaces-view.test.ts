// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  Conversation,
  MemoryItem,
  Source,
  Space,
} from "@workspace/api-client";

const createSpace = vi.fn();
const updateSpace = vi.fn();
const deleteSpace = vi.fn();
const uploadSource = vi.fn();
const deleteSource = vi.fn();
const listAgents = vi.fn();

vi.mock("../components/api", () => ({
  api: {
    createSpace: (...a: unknown[]) => createSpace(...a),
    updateSpace: (...a: unknown[]) => updateSpace(...a),
    deleteSpace: (...a: unknown[]) => deleteSpace(...a),
    uploadSource: (...a: unknown[]) => uploadSource(...a),
    deleteSource: (...a: unknown[]) => deleteSource(...a),
    listAgents: (...a: unknown[]) => listAgents(...a),
  },
}));

import { SpacesView } from "../components/views/spaces";

function space(overrides: Partial<Space> = {}): Space {
  return {
    id: "space-1",
    name: "Research",
    instructions: "Cite primary sources.",
    default_agent_id: "",
    thread_count: 1,
    source_count: 1,
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:00Z",
    ...overrides,
  };
}

function conversation(overrides: Partial<Conversation> = {}): Conversation {
  return {
    id: "conv-1",
    title: "Kestrel notes",
    subject_kind: "",
    subject_id: "",
    space_id: "space-1",
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

function source(overrides: Partial<Source> = {}): Source {
  return {
    id: "src-1",
    filename: "kestrel.md",
    media_type: "text/markdown",
    byte_size: 10,
    status: "ready",
    error: "",
    chunk_count: 1,
    conversation_id: "",
    space_id: "space-1",
    created_at: "2026-08-01T00:00:00Z",
    ...overrides,
  };
}

function memory(overrides: Partial<MemoryItem> = {}): MemoryItem {
  return {
    id: "mem-1",
    conversation_id: "conv-1",
    kind: "fact",
    content: "The kestrel figures live in table 4.",
    entity_names: [],
    message_ids: [],
    importance: 1,
    shared: true,
    space_id: "space-1",
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:00Z",
    ...overrides,
  };
}

function renderView(overrides: Partial<React.ComponentProps<typeof SpacesView>> = {}) {
  const props: React.ComponentProps<typeof SpacesView> = {
    spaces: [space()],
    spaceTemplates: [],
    conversations: [conversation(), conversation({ id: "other", space_id: "" })],
    sources: [source(), source({ id: "library", space_id: "", filename: "lib.md" })],
    memories: [],
    forgetMemory: vi.fn().mockResolvedValue(undefined),
    setError: vi.fn(),
    refreshSpaces: vi.fn().mockResolvedValue(undefined),
    onSelectConversation: vi.fn(),
    onNewThread: vi.fn(),
    onMoveThread: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
  render(React.createElement(SpacesView, props));
  return props;
}

function openSpace() {
  fireEvent.click(screen.getByRole("button", { name: /^Research/ }));
}

beforeEach(() => {
  vi.clearAllMocks();
  updateSpace.mockResolvedValue(space());
  deleteSpace.mockResolvedValue(undefined);
  uploadSource.mockResolvedValue(source());
  listAgents.mockResolvedValue([
    {
      id: "agent-1",
      name: "Archivist",
      description: "",
      instructions: "Answer as Archivist.",
      enabled: true,
      allowed_tools: null,
      created_at: "2026-08-01T00:00:00Z",
      updated_at: "2026-08-01T00:00:00Z",
    },
    {
      id: "agent-off",
      name: "Sleeper",
      description: "",
      instructions: "",
      enabled: false,
      allowed_tools: null,
      created_at: "2026-08-01T00:00:00Z",
      updated_at: "2026-08-01T00:00:00Z",
    },
  ]);
  vi.spyOn(window, "confirm").mockReturnValue(true);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("SpacesView", () => {
  it("lists spaces with their thread counts", () => {
    renderView();
    expect(screen.getByRole("button", { name: /Research 1 thread/ })).toBeTruthy();
  });

  it("shows the stored instructions and saves an edit", async () => {
    renderView();
    openSpace();
    const field = screen.getByLabelText("Space instructions") as HTMLTextAreaElement;
    expect(field.value).toBe("Cite primary sources.");
    fireEvent.change(field, { target: { value: "New rules." } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(updateSpace).toHaveBeenCalledWith("space-1", { instructions: "New rules." });
  });

  it("shows only the space's threads and opens one on click", () => {
    const props = renderView();
    openSpace();
    fireEvent.click(screen.getByRole("button", { name: /^Kestrel notes/ }));
    expect(props.onSelectConversation).toHaveBeenCalledWith("conv-1");
    expect(screen.queryByText("other")).toBeNull();
  });

  it("files an existing unspaced thread into the space", () => {
    const props = renderView({
      conversations: [
        conversation(),
        conversation({ id: "loose", title: "Loose thread", space_id: "" }),
      ],
    });
    openSpace();
    fireEvent.click(screen.getByRole("button", { name: "Add an existing thread" }));
    fireEvent.click(
      screen.getByRole("button", { name: "Add Loose thread to Research" }),
    );
    expect(props.onMoveThread).toHaveBeenCalledWith("loose", "space-1");
  });

  it("offers no add menu when every thread is already filed somewhere", () => {
    renderView({ conversations: [conversation()] });
    openSpace();
    expect(
      screen.queryByRole("button", { name: "Add an existing thread" }),
    ).toBeNull();
  });

  it("removes a thread from the space without a confirm — it is not destructive", () => {
    const props = renderView();
    openSpace();
    fireEvent.click(
      screen.getByRole("button", { name: "Remove Kestrel notes from this space" }),
    );
    expect(window.confirm).not.toHaveBeenCalled();
    expect(props.onMoveThread).toHaveBeenCalledWith("conv-1", "");
  });

  it("shows only the space's files and uploads into the space", () => {
    renderView();
    openSpace();
    expect(screen.getByText("kestrel.md")).toBeTruthy();
    expect(screen.queryByText("lib.md")).toBeNull();
    const file = new File(["body"], "new.md", { type: "text/markdown" });
    const zone = screen.getByLabelText("Space Research").querySelector(
      'input[type="file"]',
    ) as HTMLInputElement;
    fireEvent.change(zone, { target: { files: [file] } });
    expect(uploadSource).toHaveBeenCalledWith(file, "space-1");
  });

  it("starts a new thread in the space", () => {
    const props = renderView();
    openSpace();
    fireEvent.click(screen.getByRole("button", { name: "New thread" }));
    expect(props.onNewThread).toHaveBeenCalledWith("space-1");
  });

  it("deletes only after the destructive confirm, which names the stakes", () => {
    renderView();
    openSpace();
    // Delete lives in the header's actions menu now, off the stray-click path.
    fireEvent.click(screen.getByRole("button", { name: "Actions for Research" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete Research" }));
    expect(window.confirm).toHaveBeenCalledWith(
      expect.stringContaining("threads"),
    );
    expect(deleteSpace).toHaveBeenCalledWith("space-1");
  });

  it("does not delete when the confirm is declined", () => {
    (window.confirm as ReturnType<typeof vi.fn>).mockReturnValue(false);
    renderView();
    openSpace();
    fireEvent.click(screen.getByRole("button", { name: "Actions for Research" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete Research" }));
    expect(deleteSpace).not.toHaveBeenCalled();
  });

  it("picks the space's agent, offering only enabled agents", async () => {
    renderView();
    openSpace();
    const select = screen.getByLabelText("Space agent") as HTMLSelectElement;
    expect(select.value).toBe("");
    await screen.findByRole("option", { name: "Archivist" });
    expect(screen.queryByRole("option", { name: "Sleeper" })).toBeNull();
    fireEvent.change(select, { target: { value: "agent-1" } });
    expect(updateSpace).toHaveBeenCalledWith("space-1", {
      default_agent_id: "agent-1",
    });
  });

  it("renders a retired preference as itself, not as a silent reset", async () => {
    renderView({ spaces: [space({ default_agent_id: "gone-agent" })] });
    openSpace();
    await screen.findByRole("option", { name: "Archivist" });
    const select = screen.getByLabelText("Space agent") as HTMLSelectElement;
    expect(select.value).toBe("gone-agent");
    expect(screen.getByRole("option", { name: "Retired agent" })).toBeTruthy();
  });

  it("shows only the space's memory shelf and forgets through the shell", () => {
    const props = renderView({
      memories: [
        memory(),
        memory({ id: "global", space_id: "", content: "Workspace-wide fact." }),
        memory({ id: "elsewhere", space_id: "space-2", content: "Другое." }),
      ],
    });
    openSpace();
    expect(screen.getByText("The kestrel figures live in table 4.")).toBeTruthy();
    expect(screen.queryByText("Workspace-wide fact.")).toBeNull();
    expect(screen.queryByText("Другое.")).toBeNull();
    fireEvent.click(screen.getByTitle("Forget this memory"));
    expect(props.forgetMemory).toHaveBeenCalledWith(
      expect.objectContaining({ id: "mem-1" }),
    );
  });

  it("creates a space from the list pane form", () => {
    createSpace.mockResolvedValue(space({ id: "space-2", name: "Field" }));
    renderView();
    fireEvent.change(screen.getByLabelText("Space name"), {
      target: { value: "Field" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create space" }));
    expect(createSpace).toHaveBeenCalledWith({ name: "Field" });
  });
});
