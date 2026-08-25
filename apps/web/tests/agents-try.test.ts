// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentInfo } from "@workspace/api-client";

const createAgent = vi.fn();
const updateAgent = vi.fn();
const listAgents = vi.fn();
const listTools = vi.fn();
const createConversation = vi.fn();
const setConversationDefaults = vi.fn();

vi.mock("../components/api", () => ({
  api: {
    createAgent: (...a: unknown[]) => createAgent(...a),
    updateAgent: (...a: unknown[]) => updateAgent(...a),
    listAgents: (...a: unknown[]) => listAgents(...a),
    listTools: (...a: unknown[]) => listTools(...a),
    createConversation: (...a: unknown[]) => createConversation(...a),
    setConversationDefaults: (...a: unknown[]) => setConversationDefaults(...a),
  },
}));

import { AgentEditor, AgentsView } from "../components/views/agents";

/**
 * "Save & try" is a save first and a convenience second: it must obey the same
 * disabled-until-ready rule as Save, and it must hand `onTry` the id the
 * SERVER answered with — on a create there is no other place the id exists.
 */

function agentInfo(overrides: Partial<AgentInfo> = {}): AgentInfo {
  return {
    id: "agent-9",
    name: "Scout",
    description: "",
    instructions: "Find things.",
    enabled: true,
    allowed_tools: null,
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:00Z",
    ...overrides,
  };
}

function renderEditor(overrides: Partial<React.ComponentProps<typeof AgentEditor>> = {}) {
  const props: React.ComponentProps<typeof AgentEditor> = {
    agent: null,
    tools: [],
    setError: vi.fn(),
    onClose: vi.fn(),
    onSaved: vi.fn(),
    onTry: vi.fn(),
    ...overrides,
  };
  render(React.createElement(AgentEditor, props));
  return props;
}

const tryButton = () => screen.getByRole("button", { name: "Save & try" });

beforeEach(() => {
  vi.clearAllMocks();
  createAgent.mockResolvedValue(agentInfo());
  updateAgent.mockResolvedValue(agentInfo());
  listAgents.mockResolvedValue([]);
  listTools.mockResolvedValue([]);
  createConversation.mockResolvedValue({ id: "thread-1" });
  setConversationDefaults.mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
});

describe("AgentEditor Save & try", () => {
  it("stays disabled until both name and instructions are filled, like Save", () => {
    renderEditor();
    expect((tryButton() as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(screen.getByLabelText("Agent name"), {
      target: { value: "Scout" },
    });
    expect((tryButton() as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(screen.getByLabelText("System prompt"), {
      target: { value: "Find things." },
    });
    expect((tryButton() as HTMLButtonElement).disabled).toBe(false);
  });

  it("creates the agent, then fires onTry with the server's id and name", async () => {
    const props = renderEditor();
    fireEvent.change(screen.getByLabelText("Agent name"), {
      target: { value: "Scout" },
    });
    fireEvent.change(screen.getByLabelText("System prompt"), {
      target: { value: "Find things." },
    });
    fireEvent.click(tryButton());

    await waitFor(() => expect(props.onTry).toHaveBeenCalledWith("agent-9", "Scout"));
    expect(createAgent).toHaveBeenCalledTimes(1);
    // The plain-save close path must not also have fired — onTry owns the exit.
    expect(props.onSaved).not.toHaveBeenCalled();
  });

  it("on an edit, fires onTry with the updated agent's id after the update resolves", async () => {
    const existing = agentInfo({ id: "agent-4", name: "Archivist" });
    updateAgent.mockResolvedValue(existing);
    const props = renderEditor({ agent: existing });
    fireEvent.click(tryButton());

    await waitFor(() =>
      expect(props.onTry).toHaveBeenCalledWith("agent-4", "Archivist"),
    );
    expect(updateAgent).toHaveBeenCalledTimes(1);
    expect(updateAgent.mock.calls[0][0]).toBe("agent-4");
  });

  it("stays busy — no duplicate save — until the whole try path settles", async () => {
    // The double-click window this closes: `saving` clears after the save
    // alone, and a second click before the try-chat settled created the
    // agent twice.
    let releaseTry: () => void = () => undefined;
    const onTry = vi.fn(
      () => new Promise<void>((resolve) => (releaseTry = resolve)),
    );
    renderEditor({ onTry });
    fireEvent.change(screen.getByLabelText("Agent name"), {
      target: { value: "Scout" },
    });
    fireEvent.change(screen.getByLabelText("System prompt"), {
      target: { value: "Find things." },
    });
    fireEvent.click(tryButton());

    // The pressed button carries the busy copy; the wait is aria-disabled —
    // not disabled — so keyboard focus survives it.
    const busyButton = await screen.findByRole("button", { name: "Trying…" });
    expect(busyButton.getAttribute("aria-disabled")).toBe("true");
    fireEvent.click(busyButton);
    fireEvent.click(screen.getByRole("button", { name: "Create agent" }));
    expect(createAgent).toHaveBeenCalledTimes(1);

    releaseTry();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Save & try" })).toBeTruthy(),
    );
  });

  it("does not fire onTry when the save is refused", async () => {
    createAgent.mockRejectedValue(new Error("An agent with that name exists."));
    const props = renderEditor();
    fireEvent.change(screen.getByLabelText("Agent name"), {
      target: { value: "Scout" },
    });
    fireEvent.change(screen.getByLabelText("System prompt"), {
      target: { value: "Find things." },
    });
    fireEvent.click(tryButton());

    await waitFor(() =>
      expect(props.setError).toHaveBeenCalledWith("An agent with that name exists."),
    );
    expect(props.onTry).not.toHaveBeenCalled();
  });
});

describe("AgentsView try path partial failure", () => {
  it("still opens the try-chat when only the default seeding fails", async () => {
    // The thread exists and is usable; the error must say what actually
    // happened rather than un-tell the walk.
    setConversationDefaults.mockRejectedValue(new Error("nope"));
    const setError = vi.fn();
    const openConversation = vi.fn();
    render(
      React.createElement(AgentsView, { setError, openConversation }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "New agent" }));
    fireEvent.change(screen.getByLabelText("Agent name"), {
      target: { value: "Scout" },
    });
    fireEvent.change(screen.getByLabelText("System prompt"), {
      target: { value: "Find things." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save & try" }));

    await waitFor(() => expect(openConversation).toHaveBeenCalledWith("thread-1"));
    expect(setError).toHaveBeenCalledWith(
      "Saved and opened the try-chat — but the agent could not be set as " +
        "its default; pick it in the composer.",
    );
  });
});
