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

  it("still shows the banner before anything has run", () => {
    render(createElement(ChatView, { ...BASE, approval: approval(false) }));
    expect(screen.getByRole("button", { name: "Ask me first" })).toBeTruthy();
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

  // The provenance gate's NOTE, not a second toggle: it is workspace-wide and
  // owner-only, so a per-member checkbox here would promise authority the
  // member does not have. Guarded and optional, so every mount above — which
  // passes no `taint` at all — renders byte-identically.
  const GATE_NOTE = /makes writes and network calls ask first/;

  it("says nothing about the gate when the prop is absent", () => {
    menu(false, () => undefined);
    fireEvent.click(screen.getByRole("button", { name: "Workspace settings" }));
    expect(screen.queryByText(GATE_NOTE)).toBeNull();
  });

  it("stays quiet when the gate is off for this workspace", () => {
    render(
      createElement(WorkspaceSettingsMenu, {
        activeGroup: "chat" as never,
        open: () => undefined,
        digest: null,
        onDigestChange: () => undefined,
        safeMode: false,
        onSafeModeChange: () => undefined,
        taint: { enabled: false },
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Workspace settings" }));
    expect(screen.queryByText(GATE_NOTE)).toBeNull();
  });

  it("names the gate, and where it is changed, when it is armed", () => {
    render(
      createElement(WorkspaceSettingsMenu, {
        activeGroup: "chat" as never,
        open: () => undefined,
        digest: null,
        onDigestChange: () => undefined,
        safeMode: false,
        onSafeModeChange: () => undefined,
        taint: { enabled: true },
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Workspace settings" }));
    // Both halves matter: what happens, and that this checkbox is not the
    // last word on it.
    expect(screen.getByText(GATE_NOTE)).toBeTruthy();
    expect(screen.getByText(/Rules & policies/)).toBeTruthy();
  });
});

describe("the Memory preference toggle", () => {
  // The Safe-mode helper's shape, one preference over: same menu, same slot
  // convention (per-member prefs beside the member's own controls).
  function menu(
    memoryEnabled: boolean | null,
    onChange: (enabled: boolean) => void = () => undefined,
  ) {
    return render(
      createElement(WorkspaceSettingsMenu, {
        activeGroup: "chat" as never,
        open: () => undefined,
        digest: null,
        onDigestChange: () => undefined,
        safeMode: false,
        onSafeModeChange: () => undefined,
        memoryEnabled,
        onMemoryEnabledChange: onChange,
      }),
    );
  }

  const LABEL = /Remember things from my chats/;

  it("never renders before the preference has been read", () => {
    // Null is "bootstrap has not landed", not "off": a checkbox shown with a
    // guessed value would misreport what the assistant is learning, for the
    // one preference where that is the whole subject.
    menu(null);
    fireEvent.click(screen.getByRole("button", { name: "Workspace settings" }));
    expect(screen.queryByRole("checkbox", { name: LABEL })).toBeNull();
    expect(screen.queryByText("Memory")).toBeNull();
  });

  it("stays hidden on a bare mount, so older call sites stand unchanged", () => {
    render(
      createElement(WorkspaceSettingsMenu, {
        activeGroup: "chat" as never,
        open: () => undefined,
        digest: null,
        onDigestChange: () => undefined,
        safeMode: false,
        onSafeModeChange: () => undefined,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Workspace settings" }));
    expect(screen.queryByRole("checkbox", { name: LABEL })).toBeNull();
  });

  it("says what ON does — including the temporary-chat carve-out", () => {
    menu(true);
    fireEvent.click(screen.getByRole("button", { name: "Workspace settings" }));
    const checkbox = screen.getByRole("checkbox", { name: LABEL }) as HTMLInputElement;
    expect(checkbox.checked).toBe(true);
    expect(
      screen.getByText(/Temporary chats never do either/),
    ).toBeTruthy();
  });

  it("says what OFF keeps — the remember tool, and everything already learned", () => {
    menu(false);
    fireEvent.click(screen.getByRole("button", { name: "Workspace settings" }));
    // Off is not an erasure and not a muzzle: both boundaries are named, or a
    // member flips it expecting a deletion this toggle does not perform.
    expect(
      screen.getByText(/Asking the assistant to remember something still works/),
    ).toBeTruthy();
    expect(
      screen.getByText(/keeps what it already learned/),
    ).toBeTruthy();
  });

  it("reports the click", () => {
    const seen: boolean[] = [];
    menu(true, (enabled) => seen.push(enabled));
    fireEvent.click(screen.getByRole("button", { name: "Workspace settings" }));
    fireEvent.click(screen.getByRole("checkbox", { name: LABEL }));
    expect(seen).toEqual([false]);
  });

  it("renders in the preference slot, after Safe mode", () => {
    menu(true);
    fireEvent.click(screen.getByRole("button", { name: "Workspace settings" }));
    const notes = screen
      .getAllByText(/^(Safe mode|Memory)$/)
      .map((node) => node.textContent);
    expect(notes).toEqual(["Safe mode", "Memory"]);
  });
});
