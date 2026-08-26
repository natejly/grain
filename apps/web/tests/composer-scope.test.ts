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
 *   uses it).
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
  it("shows the mode control wherever an approval bundle is passed", () => {
    render(createElement(ChatView, { ...BASE, approval: APPROVAL }));
    expect(
      screen.getByRole("button", { name: /^Approval mode:/ }),
    ).toBeTruthy();
  });

  it("renders no mode control where there is no thread to govern", () => {
    render(createElement(ChatView, { ...BASE }));
    expect(screen.queryByRole("button", { name: /^Approval mode:/ })).toBeNull();
  });
});
