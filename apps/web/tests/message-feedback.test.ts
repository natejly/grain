// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { createElement, createRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Message } from "@workspace/api-client";
import { ChatView, type ChatViewProps } from "../components/views/chat";

/**
 * The thumbs beside an assistant answer. Where they render is the contract:
 * assistant messages only, only when the mount wired the handler (side-panel
 * mounts stand unchanged), active state read from the transcript's own
 * `my_feedback`, and a thumbs-down collects its optional note in a popover
 * before anything posts.
 */

function message(overrides: Partial<Message> = {}): Message {
  return {
    id: "m-answer",
    run_id: "run-1",
    role: "assistant",
    content: "here is the answer",
    citations: [],
    citation_report: null,
    sender_id: "",
    sender_name: "",
    created_at: "2026-09-20T00:00:00Z",
    ...overrides,
  };
}

const BASE: ChatViewProps = {
  messages: [],
  sources: [],
  agentCalls: [],
  apps: [],
  draft: "",
  setDraft: () => undefined,
  activeRun: null,
  runStatus: "",
  budgetPark: null,
  submitPrompt: async () => undefined,
  cancelActiveRun: async () => undefined,
  regenerate: async () => undefined,
  decideAgentCall: async () => undefined,
  openCitation: async () => undefined,
  endRef: createRef<HTMLDivElement>(),
};

afterEach(cleanup);

describe("message feedback thumbs", () => {
  it("render on assistant messages only", () => {
    render(
      createElement(ChatView, {
        ...BASE,
        messages: [
          message(),
          message({ id: "m-prompt", role: "user", content: "the question" }),
        ],
        feedback: vi.fn().mockResolvedValue(undefined),
      }),
    );
    expect(screen.getAllByRole("button", { name: "Good answer" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Bad answer" })).toHaveLength(1);
  });

  it("never render on the streaming placeholder — its id names no server row", () => {
    // The synthetic `streaming-<runId>` row a live run renders under: a
    // thumb clicked there would POST a fake id, 404, and roll back with the
    // red toast. The gate waits for the persisted message that replaces it.
    render(
      createElement(ChatView, {
        ...BASE,
        activeRun: "run-1",
        messages: [message({ id: "streaming-run-1", content: "half an ans" })],
        feedback: vi.fn().mockResolvedValue(undefined),
      }),
    );
    expect(screen.queryByRole("button", { name: "Good answer" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Bad answer" })).toBeNull();
  });

  it("stay out of the tree when no handler is wired — side panels stand", () => {
    render(createElement(ChatView, { ...BASE, messages: [message()] }));
    expect(screen.queryByRole("button", { name: "Good answer" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Bad answer" })).toBeNull();
  });

  it("show the viewer's own recorded verdict as the active state", () => {
    render(
      createElement(ChatView, {
        ...BASE,
        messages: [message({ my_feedback: "down" })],
        feedback: vi.fn().mockResolvedValue(undefined),
      }),
    );
    const down = screen.getByRole("button", { name: "Bad answer" });
    expect(down.getAttribute("aria-pressed")).toBe("true");
    expect(
      screen
        .getByRole("button", { name: "Good answer" })
        .getAttribute("aria-pressed"),
    ).toBe("false");
  });

  it("posts an up verdict immediately, with no note", () => {
    const feedback = vi.fn().mockResolvedValue(undefined);
    render(
      createElement(ChatView, { ...BASE, messages: [message()], feedback }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Good answer" }));
    expect(feedback).toHaveBeenCalledWith("m-answer", "up", "");
  });

  it("collects the optional note before a down verdict posts", () => {
    const feedback = vi.fn().mockResolvedValue(undefined);
    render(
      createElement(ChatView, { ...BASE, messages: [message()], feedback }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Bad answer" }));
    // Nothing posted yet: the popover is the chance to say why.
    expect(feedback).not.toHaveBeenCalled();
    fireEvent.change(
      screen.getByRole("textbox", { name: "What went wrong? (optional)" }),
      { target: { value: "misses the point" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(feedback).toHaveBeenCalledWith("m-answer", "down", "misses the point");
    // The popover closed with the send.
    expect(
      screen.queryByRole("textbox", { name: "What went wrong? (optional)" }),
    ).toBeNull();
  });

  it("sends a down verdict with an empty note when nothing is typed", () => {
    const feedback = vi.fn().mockResolvedValue(undefined);
    render(
      createElement(ChatView, { ...BASE, messages: [message()], feedback }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Bad answer" }));
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(feedback).toHaveBeenCalledWith("m-answer", "down", "");
  });
});
