import { cleanup, render, screen } from "@testing-library/react";
import { createElement, createRef } from "react";
import { afterEach, describe, expect, it } from "vitest";
import type { AgentToolCall, Message } from "@workspace/api-client";
import { ChatView, type ChatViewProps } from "../components/views/chat";
import { FIRST_RUN_KEY, serializeSeen } from "../components/views/first-run";

/**
 * The approval-loop caption teaches exactly once. The first proposed call a
 * user ever meets explains itself — "nothing happens until you decide" — and
 * every later one trusts the lesson took. The card renders from two sites
 * (the per-message loop and the trailing live loop), so the invariant pinned
 * here is one caption across both, chosen by ChatView, and none at all once
 * `grain.seen` carries the mark.
 */

function call(overrides: Partial<AgentToolCall> = {}): AgentToolCall {
  return {
    id: "call-1",
    run_id: "run-1",
    conversation_id: "conv-1",
    name: "fs_write",
    arguments_json: "{}",
    proposal_preview: "",
    status: "proposed",
    result_preview: "",
    error: "",
    latency_ms: 0,
    artifacts: [],
    approved_by_mode: "",
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function message(overrides: Partial<Message> = {}): Message {
  return {
    id: "m1",
    run_id: "run-1",
    role: "user",
    content: "please write the file",
    citations: [],
    citation_report: null,
    sender_id: "user-1",
    sender_name: "Me",
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

const APPROVAL = {
  mode: "ask_writes" as const,
  setMode: async () => undefined,
  conversationId: "conv-1",
  conversationTitle: "Widget",
};

const BASE: ChatViewProps = {
  messages: [message()],
  sources: [],
  agentCalls: [call()],
  apps: [],
  draft: "",
  setDraft: () => undefined,
  // The proposed call has no assistant message yet, so it renders from the
  // live-calls site — the exact spot a user first meets the approval loop.
  activeRun: "run-1",
  runStatus: "",
  budgetPark: null,
  submitPrompt: async () => undefined,
  cancelActiveRun: async () => undefined,
  regenerate: async () => undefined,
  decideAgentCall: async () => undefined,
  openCitation: async () => undefined,
  endRef: createRef<HTMLDivElement>(),
  approval: APPROVAL,
};

const CAPTION = /this is the approval loop/;

function view(props: Partial<ChatViewProps> = {}) {
  return render(createElement(ChatView, { ...BASE, ...props }));
}

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

describe("the approval-loop first-run caption", () => {
  it("rides the first pending card when unseen, and writes the mark", () => {
    view();
    expect(screen.getAllByText(CAPTION)).toHaveLength(1);
    // Marked from the effect only once the caption actually rendered, so the
    // next session — not this render — is the one that goes without it.
    expect(window.localStorage.getItem(FIRST_RUN_KEY)).toContain("approval-loop");
  });

  it("is one caption across a stack of pending cards", () => {
    view({
      agentCalls: [call(), call({ id: "call-2", name: "sandbox_run" })],
    });
    expect(screen.getAllByText(CAPTION)).toHaveLength(1);
  });

  it("stays quiet once grain.seen carries the mark", () => {
    window.localStorage.setItem(
      FIRST_RUN_KEY,
      serializeSeen(new Set(["approval-loop"])),
    );
    view();
    expect(screen.queryByText(CAPTION)).toBeNull();
  });

  it("never spends the mark where there is no approval bundle", () => {
    view({ approval: undefined });
    expect(screen.queryByText(CAPTION)).toBeNull();
    expect(window.localStorage.getItem(FIRST_RUN_KEY)).toBeNull();
  });

  it("never spends the mark in a subject panel — approval present, teaching opted out", () => {
    // Subject panels DO carry the approval bundle now (the mode control),
    // and opt out of teaching with showStarter={false}. A first-ever proposal
    // in a narrow document panel must not consume the caption's one showing —
    // the primary chat is where the lesson lands.
    view({ showStarter: false });
    expect(screen.queryByText(CAPTION)).toBeNull();
    expect(window.localStorage.getItem(FIRST_RUN_KEY)).toBeNull();
  });
});
