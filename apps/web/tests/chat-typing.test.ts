// @vitest-environment jsdom
import { cleanup, render } from "@testing-library/react";
import { createElement, createRef } from "react";
import { afterEach, describe, expect, it } from "vitest";
import type { CoworkingPresence } from "@workspace/api-client";
import type { CoworkingState } from "../components/use-coworking";
import { ChatView, type ChatViewProps, typersOn, typingLine } from "../components/views/chat";

/**
 * The receiving end of chat typing presence, and where the pointer layer is
 * allowed to exist. The sender side shipped with the coworking work
 * (use-workspace.ts beats `{typing}` on `conversation:<id>`); what is pinned
 * here is who the line COUNTS — never the viewer, never another surface —
 * and that a personal thread mounts neither the line nor the cursor layer,
 * so it never even emits a pointer beat.
 */

function presence(overrides: Partial<CoworkingPresence>): CoworkingPresence {
  return {
    actor_id: "user-2",
    actor_kind: "user",
    actor_label: "Bo",
    surface: "conversation:c1",
    state: { typing: true },
    updated_at: "2026-09-20T10:00:00Z",
    ...overrides,
  };
}

describe("typersOn", () => {
  it("keeps only other people typing on this conversation", () => {
    const rows = [
      presence({}),
      // The viewer's own beat — their composer knows they are typing.
      presence({ actor_id: "user-1", actor_label: "Me" }),
      // A teammate typing in a different thread.
      presence({ actor_id: "user-3", surface: "conversation:c2" }),
      // Present here, but reading rather than typing.
      presence({ actor_id: "user-4", state: { typing: false } }),
      // A document surface must never leak into the chat line.
      presence({ actor_id: "user-5", surface: "document:d1" }),
    ];
    const typers = typersOn(rows, "conversation:c1", "user-1");
    expect(typers.map((row) => row.actor_id)).toEqual(["user-2"]);
  });
});

describe("typingLine", () => {
  it.each([
    [[], ""],
    [["Alice"], "Alice is typing…"],
    [["Alice", "Bob"], "Alice and Bob are typing…"],
    [["Alice", "Bob", "Cy"], "Several people are typing…"],
  ])("words %j as %j", (names, expected) => {
    expect(typingLine(names as string[])).toBe(expected);
  });
});

// --- Where the mounts are allowed to exist ----------------------------------

const noop = () => undefined;

function coworking(presences: CoworkingPresence[]): CoworkingState {
  return {
    runs: [],
    presences,
    othersOn: (surface) =>
      presences.filter((row) => row.surface === surface && row.actor_id !== "user-1"),
    report: noop,
    reportPointer: noop,
    leave: noop,
  };
}

const BASE: ChatViewProps = {
  messages: [],
  sources: [],
  agentCalls: [],
  apps: [],
  draft: "",
  setDraft: noop,
  activeRun: null,
  runStatus: "",
  budgetPark: null,
  submitPrompt: async () => undefined,
  cancelActiveRun: async () => undefined,
  regenerate: async () => undefined,
  decideAgentCall: async () => undefined,
  openCitation: async () => undefined,
  endRef: createRef<HTMLDivElement>(),
  viewerId: "user-1",
  coworking: coworking([presence({})]),
  conversationId: "c1",
};

afterEach(cleanup);

describe("the shared-thread mounts", () => {
  it("shows the typing line and the cursor layer on a shared thread", () => {
    const { container } = render(
      createElement(ChatView, { ...BASE, sharedThread: true }),
    );
    expect(container.querySelector(".chat-typing")?.textContent).toBe(
      "Bo is typing…",
    );
    expect(container.querySelector(".live-cursor-layer")).not.toBeNull();
  });

  it("keeps the line rendered — empty — while nobody types, so the composer never jumps", () => {
    const { container } = render(
      createElement(ChatView, {
        ...BASE,
        sharedThread: true,
        coworking: coworking([]),
      }),
    );
    expect(container.querySelector(".chat-typing")?.textContent).toBe("");
  });

  it("mounts neither on a personal thread, even with the channel wired", () => {
    const { container } = render(
      createElement(ChatView, { ...BASE, sharedThread: false }),
    );
    expect(container.querySelector(".chat-typing")).toBeNull();
    expect(container.querySelector(".live-cursor-layer")).toBeNull();
  });
});
