import { cleanup, render, screen } from "@testing-library/react";
import { createElement, createRef } from "react";
import { afterEach, describe, expect, it } from "vitest";
import type { AgentToolCall } from "@workspace/api-client";
import { ChatView, type ChatViewProps } from "../components/views/chat";

/**
 * `DEV_UNRESTRICTED_AGENT` has to be *visible* while it is on.
 *
 * The failure mode is not switching it on. It is forgetting it is on — three
 * days later, in a panel you reopened, while pasting in something you did not
 * write — with every tool available and nothing parking. So the warning is
 * asserted here on the rendered text, not on the presence of an element: a
 * banner that appears with the wrong words in it, or that says a per-thread
 * bypass is on when the whole deployment is unguarded, is the same failure
 * wearing a passing test.
 */

function call(overrides: Partial<AgentToolCall> = {}): AgentToolCall {
  return {
    id: "call-1",
    run_id: "run-1",
    conversation_id: "conv-1",
    name: "fs_write",
    arguments_json: "{}",
    proposal_preview: "",
    status: "succeeded",
    result_preview: "wrote App.tsx",
    error: "",
    latency_ms: 4,
    artifacts: [],
    approved_by_mode: "auto_writes",
    created_at: "2026-01-01T00:00:00Z",
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

const THREAD_BYPASS = {
  mode: "auto_writes" as const,
  setMode: async () => undefined,
  conversationId: "conv-1",
  conversationTitle: "Widget",
};

function view(props: Partial<ChatViewProps> = {}) {
  return render(createElement(ChatView, { ...BASE, ...props }));
}

afterEach(cleanup);

describe("the development bypass says so", () => {
  it("names the mode, not a thread, and offers no off switch", () => {
    view({ unrestricted: { conversationId: "conv-1" } });
    expect(screen.getByText("Development mode: no approvals, every tool")).toBeTruthy();
    // No "Turn off": this is an environment variable the server refuses outside
    // development, not a per-thread setting a click can change, and a button
    // that could not do what it said would be worse than none.
    expect(screen.queryByRole("button", { name: "Turn off" })).toBeNull();
  });

  it("counts only what actually went through unreviewed, in this thread", () => {
    view({
      unrestricted: { conversationId: "conv-1" },
      agentCalls: [
        call(),
        // Another thread's call, and a call a standing policy allowed rather
        // than the bypass. Neither belongs in this thread's trail.
        call({ id: "call-2", conversation_id: "conv-2" }),
        call({ id: "call-3", approved_by_mode: "" }),
      ],
    });
    expect(screen.getByText(/1 call went through unreviewed/)).toBeTruthy();
  });

  it("says nothing has yet when nothing has", () => {
    view({ unrestricted: { conversationId: "conv-1" } });
    expect(screen.getByText(/Nothing has gone through unreviewed yet/)).toBeTruthy();
  });

  it("outranks the per-thread bypass banner rather than stacking with it", () => {
    view({ unrestricted: { conversationId: "conv-1" }, approval: THREAD_BYPASS });
    // The wider fact wins: showing the thread-level banner beside it would
    // offer a "Turn off" that does not turn this off.
    expect(screen.queryByText(/Auto-approving writes in/)).toBeNull();
    expect(screen.getByText("Development mode: no approvals, every tool")).toBeTruthy();
  });

  it("is absent when the flag is off, whatever the thread's own mode is", () => {
    view({ approval: THREAD_BYPASS });
    expect(screen.queryByText(/Development mode/)).toBeNull();
    expect(screen.getByText(/Auto-approving writes in/)).toBeTruthy();
  });
});
