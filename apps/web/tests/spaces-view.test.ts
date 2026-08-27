// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Conversation, Source, Space } from "@workspace/api-client";

const createSpace = vi.fn();
const updateSpace = vi.fn();
const deleteSpace = vi.fn();
const uploadSource = vi.fn();
const deleteSource = vi.fn();

vi.mock("../components/api", () => ({
  api: {
    createSpace: (...a: unknown[]) => createSpace(...a),
    updateSpace: (...a: unknown[]) => updateSpace(...a),
    deleteSpace: (...a: unknown[]) => deleteSpace(...a),
    uploadSource: (...a: unknown[]) => uploadSource(...a),
    deleteSource: (...a: unknown[]) => deleteSource(...a),
  },
}));

import { SpacesView } from "../components/views/spaces";

function space(overrides: Partial<Space> = {}): Space {
  return {
    id: "space-1",
    name: "Research",
    instructions: "Cite primary sources.",
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

function renderView(overrides: Partial<React.ComponentProps<typeof SpacesView>> = {}) {
  const props: React.ComponentProps<typeof SpacesView> = {
    spaces: [space()],
    spaceTemplates: [],
    conversations: [conversation(), conversation({ id: "other", space_id: "" })],
    sources: [source(), source({ id: "library", space_id: "", filename: "lib.md" })],
    setError: vi.fn(),
    refreshSpaces: vi.fn().mockResolvedValue(undefined),
    onSelectConversation: vi.fn(),
    onNewThread: vi.fn(),
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
    fireEvent.click(screen.getByRole("button", { name: /Kestrel notes/ }));
    expect(props.onSelectConversation).toHaveBeenCalledWith("conv-1");
    expect(screen.queryByText("other")).toBeNull();
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
    fireEvent.click(screen.getByRole("button", { name: "Delete Research" }));
    expect(deleteSpace).not.toHaveBeenCalled();
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
