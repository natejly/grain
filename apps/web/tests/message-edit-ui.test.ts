// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { createElement, createRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Message } from "@workspace/api-client";
import { ChatView, type ChatViewProps } from "../components/views/chat";

/**
 * The pencil's rules, asserted where they render. An edit is a TRUNCATION —
 * the server deletes everything after the message — so where the affordance
 * appears is a correctness question, not chrome: never on an aside (the API
 * 422s), never on a teammate's shared-thread prompt (403), and a refused edit
 * must keep the rewritten words on screen rather than feed them to a toast.
 */

function message(overrides: Partial<Message> = {}): Message {
  return {
    id: "m1",
    run_id: "run-1",
    role: "user",
    content: "original words",
    citations: [],
    citation_report: null,
    sender_id: "user-1",
    sender_name: "Me",
    created_at: "2026-08-23T00:00:00Z",
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

describe("the message-edit pencil", () => {
  it("rides the viewer's own prompt and swaps in an editor", async () => {
    const editMessage = vi.fn().mockResolvedValue(true);
    render(
      createElement(ChatView, {
        ...BASE,
        messages: [message()],
        editMessage,
        viewerId: "user-1",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: /^Edit:/ }));
    const editor = screen.getByRole("textbox", { name: "Edit message" });
    expect((editor as HTMLTextAreaElement).value).toBe("original words");
    // The editor says what saving does BEFORE the button is pressed — the
    // truncation must not be discovered from its result — and the commit
    // button's own name carries the re-run.
    expect(
      screen.getByText(/Saving re-runs the thread from here/),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: "Save & re-run" })).toBeTruthy();
  });

  it("never appears on an aside — there is no turn to re-run", () => {
    render(
      createElement(ChatView, {
        ...BASE,
        messages: [message({ run_id: "" })],
        editMessage: async () => true,
        viewerId: "user-1",
      }),
    );
    expect(screen.queryByRole("button", { name: /^Edit:/ })).toBeNull();
  });

  it("never appears on a teammate's message in a shared thread", () => {
    render(
      createElement(ChatView, {
        ...BASE,
        messages: [message({ sender_id: "user-2", sender_name: "Them" })],
        sharedThread: true,
        editMessage: async () => true,
        viewerId: "user-1",
      }),
    );
    expect(screen.queryByRole("button", { name: /^Edit:/ })).toBeNull();
  });

  it("refuses a second submit while the first is in flight", async () => {
    // activeRun only becomes true AFTER the edit's round trip, so without an
    // in-flight flag a held Enter's key repeat fires concurrent truncations
    // at the same pivot.
    let release: (value: boolean) => void = () => undefined;
    const editMessage = vi.fn(
      () => new Promise<boolean>((resolve) => (release = resolve)),
    );
    render(
      createElement(ChatView, {
        ...BASE,
        messages: [message()],
        editMessage,
        viewerId: "user-1",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: /^Edit:/ }));
    const save = screen.getByRole("button", { name: "Save & re-run" });
    fireEvent.click(save);
    fireEvent.click(save);
    fireEvent.click(save);
    expect(editMessage).toHaveBeenCalledTimes(1);
    release(true);
    await Promise.resolve();
  });

  it("keeps the editor open when the server refuses, so the words survive", async () => {
    const editMessage = vi.fn().mockResolvedValue(false);
    render(
      createElement(ChatView, {
        ...BASE,
        messages: [message()],
        editMessage,
        viewerId: "user-1",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: /^Edit:/ }));
    const editor = screen.getByRole("textbox", { name: "Edit message" });
    fireEvent.change(editor, { target: { value: "rewritten" } });
    fireEvent.click(screen.getByRole("button", { name: "Save & re-run" }));
    // Let the rejected promise settle.
    await Promise.resolve();
    await Promise.resolve();
    expect(editMessage).toHaveBeenCalledWith("m1", "rewritten");
    expect(screen.getByRole("textbox", { name: "Edit message" })).toBeTruthy();
  });
});
