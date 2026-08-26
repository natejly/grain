import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { createElement, createRef } from "react";
import { afterEach, describe, expect, it } from "vitest";
import type { AgentToolCall } from "@workspace/api-client";
import { ChatView, type ChatViewProps } from "../components/views/chat";
import { WorkspaceSettingsMenu } from "../components/settings-menu";
import { APPROVAL_MODES, describeMode, isBypass } from "../components/views/approval-format";

/**
 * Safe mode, and the banner volume that has to go with it.
 *
 * The default is now agentic: writes run and the trail says what ran. That puts
 * weight on two things this file guards.
 *
 * The trail must not go quiet just because the mode became the default — the
 * whole honesty of an agentic default is that what ran is on screen. And the
 * ALARM must not become the default, because an alarm on every thread is
 * furniture within a week, and then it is not there for the member who turned
 * Safe mode on and is being ignored.
 */

afterEach(cleanup);

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
    assigned_to: "",
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

function approval(safeMode: boolean, mode: "auto_writes" | "ask_writes" = "auto_writes") {
  return {
    mode,
    setMode: async () => undefined,
    conversationId: "conv-1",
    conversationTitle: "Widget",
    safeMode,
  };
}

describe("the auto-approve banner's volume", () => {
  it("stays quiet when acting on its own is what this member asked for", () => {
    render(
      createElement(ChatView, {
        ...BASE,
        agentCalls: [call()],
        approval: approval(false),
      }),
    );
    expect(screen.queryByText(/Auto-approving writes in/)).toBeNull();
    expect(screen.getByText(/1 call ran without asking/)).toBeTruthy();
  });

  it("still shows the trail before anything has run", () => {
    render(createElement(ChatView, { ...BASE, approval: approval(false) }));
    expect(screen.getByText(/Acting without asking/)).toBeTruthy();
  });

  it("shouts when the member asked to be asked and is not being", () => {
    render(
      createElement(ChatView, {
        ...BASE,
        agentCalls: [call()],
        approval: approval(true),
      }),
    );
    expect(screen.getByText(/Auto-approving writes in “Widget”/)).toBeTruthy();
  });

  it("says nothing at all when the thread is not auto-approving", () => {
    render(
      createElement(ChatView, {
        ...BASE,
        agentCalls: [call()],
        approval: approval(false, "ask_writes"),
      }),
    );
    expect(screen.queryByText(/Auto-approving writes in/)).toBeNull();
    expect(screen.queryByText(/ran without asking/)).toBeNull();
  });

  it("offers a way back to asking even in the quiet treatment", () => {
    render(
      createElement(ChatView, {
        ...BASE,
        agentCalls: [call()],
        approval: approval(false),
      }),
    );
    // The argument for an agentic default is that stopping it is always one
    // click away — including on the thread where nothing looks alarming.
    expect(screen.getByRole("button", { name: "Ask me first" })).toBeTruthy();
  });
});

describe("the mode picker", () => {
  it("offers the agentic mode first, since that is what a new thread is", () => {
    expect(APPROVAL_MODES[0]?.mode).toBe("auto_writes");
  });

  it("never lets an unknown mode resolve to a bypass", () => {
    // The regression this exists for: the fallback used to be
    // APPROVAL_MODES[0], so reordering the picker silently made every
    // unrecognised mode render — and answer isBypass — as a bypass.
    expect(describeMode("something_new").mode).toBe("ask_writes");
    expect(isBypass("something_new")).toBe(false);
    expect(isBypass("")).toBe(false);
  });
});

describe("the Safe mode toggle", () => {
  function menu(safeMode: boolean, onChange: (enabled: boolean) => void) {
    return render(
      createElement(WorkspaceSettingsMenu, {
        activeGroup: "chat" as never,
        open: () => undefined,
        digest: null,
        onDigestChange: () => undefined,
        safeMode,
        onSafeModeChange: onChange,
      }),
    );
  }

  it("is off by default and reports the agentic posture", () => {
    menu(false, () => undefined);
    fireEvent.click(screen.getByRole("button", { name: "Workspace settings" }));
    const checkbox = screen.getByRole("checkbox", {
      name: /Ask me before the assistant writes anything/,
    }) as HTMLInputElement;
    expect(checkbox.checked).toBe(false);
    expect(screen.getByText(/New threads act on their own/)).toBeTruthy();
  });

  it("says what it governs — and what it does not — when it is on", () => {
    menu(true, () => undefined);
    fireEvent.click(screen.getByRole("button", { name: "Workspace settings" }));
    // Naming the boundary is the point: a member who flips this expecting the
    // thread on screen to change is told here rather than by it not changing.
    expect(screen.getByText(/Threads already open keep the mode they are in/)).toBeTruthy();
  });

  it("reports the click", () => {
    const seen: boolean[] = [];
    menu(false, (enabled) => seen.push(enabled));
    fireEvent.click(screen.getByRole("button", { name: "Workspace settings" }));
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: /Ask me before the assistant writes anything/,
      }),
    );
    expect(seen).toEqual([true]);
  });
});
