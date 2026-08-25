// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { createElement, createRef } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { ChatView, type ChatViewProps } from "../components/views/chat";

/**
 * Phase 4 trust rules on the composer, asserted where they render:
 *
 * - a menu names its scope — the model/effort/agent pickers say "this thread",
 *   because a choice that persists somewhere unstated is the class of surprise
 *   the Rules work exists to remove;
 * - the subject panels now carry the approval-mode control (they used to hide
 *   it, leaving a subject thread's mode unreadable from the one surface that
 *   uses it), but they do NOT inherit the rail chat's starter cards — an empty
 *   panel beside a document is scoped to that document.
 */

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

const APPROVAL = {
  mode: "ask_writes" as const,
  setMode: async () => undefined,
  conversationId: "conv-1",
  conversationTitle: "Chat about this document",
};

const TURN_CONTROLS = {
  models: ["gpt-a", "gpt-b"],
  efforts: ["low", "high"],
  model: "",
  setModel: () => undefined,
  effort: "low",
  setEffort: () => undefined,
  fast: false,
  setFast: () => undefined,
};

afterEach(cleanup);

describe("composer scope labels", () => {
  it("names the thread as the scope on the model and effort pickers", () => {
    render(createElement(ChatView, { ...BASE, turnControls: TURN_CONTROLS }));
    expect(
      screen.getByRole("combobox", { name: "Model · this thread" }),
    ).toBeTruthy();
    expect(
      screen.getByRole("combobox", { name: "Reasoning effort · this thread" }),
    ).toBeTruthy();
  });
});

describe("subject panels with the approval control", () => {
  it("shows the mode control without the rail chat's starter cards", () => {
    render(
      createElement(ChatView, { ...BASE, approval: APPROVAL, showStarter: false }),
    );
    expect(
      screen.getByRole("button", { name: /^Approval mode:/ }),
    ).toBeTruthy();
    expect(screen.queryByText("Ask, and approve what the agent does")).toBeNull();
  });

  it("keeps teaching in surfaces that do not opt out", () => {
    render(createElement(ChatView, { ...BASE, approval: APPROVAL }));
    expect(
      screen.getByText("Ask, and approve what the agent does"),
    ).toBeTruthy();
  });
});
