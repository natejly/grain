// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import type { AgentToolCall, Message } from "@workspace/api-client";
import { createElement, createRef } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { ChatView, type ChatViewProps } from "../components/views/chat";
import { describeGate } from "../components/views/approval-format";

/**
 * The provenance gate's copy, and where it lands.
 *
 * Two claims, and both are about being honest rather than about being
 * informative:
 *
 *  - `describeGate` renders NOTHING it cannot fully parse. The reason is a
 *    machine string the server owns, so a build that meets a class or an action
 *    it does not know must say nothing at all — garbage inside an approval card
 *    is worse than an unexplained card, because the reader trusts it.
 *  - The sentence says "This turn read …", never "This block triggered …".
 *    Nothing proves which piece of context caused which tool call; the enforced
 *    guarantee is turn-level, and copy that implied otherwise would be us
 *    misleading the exact person deciding the card.
 */

afterEach(cleanup);

describe("describeGate", () => {
  it("says nothing it cannot fully parse", () => {
    expect(describeGate("")).toBe("");
    expect(describeGate("garbage")).toBe("");
    expect(describeGate("taint:web_fetch")).toBe("");
    expect(describeGate("taint:web_fetch:write:extra")).toBe("");
    expect(describeGate("screen:web_fetch:write")).toBe("");
    // A class this build does not know poisons the whole sentence rather than
    // being dropped: listing two of three sources would understate what the
    // turn read, which is the one direction this copy must not be wrong in.
    expect(describeGate("taint:quantum_oracle:write")).toBe("");
    expect(describeGate("taint:mcp_result,quantum_oracle:write")).toBe("");
    // An action this build does not know, likewise.
    expect(describeGate("taint:mcp_result:teleport")).toBe("");
    expect(describeGate("taint::write")).toBe("");
  });

  it("names both the class and the action", () => {
    const prose = describeGate("taint:mcp_result:egress");
    expect(prose).toContain("a result from a connected MCP server");
    expect(prose).toContain("reaches the network");
    expect(describeGate("taint:web_fetch:write")).toContain("changes something");
  });

  it("claims a turn read something, never that a block caused something", () => {
    const prose = describeGate("taint:web_fetch:write");
    expect(prose.startsWith("This turn read ")).toBe(true);
    expect(prose).not.toContain("triggered");
    expect(prose).not.toContain("caused");
  });

  it("renders several classes in the order the server sorted them", () => {
    expect(describeGate("taint:mcp_result,web_fetch:write")).toBe(
      "This turn read a result from a connected MCP server and a page fetched " +
        "from the web. Because this call changes something, it is waiting for " +
        "you even though the thread is set to act on its own.",
    );
  });

  it("says when the server had to drop classes to fit the column", () => {
    // `gate_reason` holds 64 characters and the names sort alphabetically, so
    // the tail it cuts is always `web_fetch`/`workspace_chunk` — the classes
    // this card most needs to name. A short list rendered as a complete
    // sentence would understate what the turn read.
    const prose = describeGate(
      "taint:mcp_result,memory_item,sandbox_output,+2:egress",
    );
    expect(prose).toContain("2 other kinds of source");
    expect(prose).toContain("reaches the network");
    expect(describeGate("taint:mcp_result,+1:write")).toContain(
      "one other kind of source",
    );
    // Still nothing it cannot parse: a marker with no classes beside it, or a
    // malformed one, renders nothing at all.
    expect(describeGate("taint:+2:write")).toBe("");
    expect(describeGate("taint:mcp_result,+x:write")).toBe("");
  });
});

function call(overrides: Partial<AgentToolCall> = {}): AgentToolCall {
  return {
    id: "call-1",
    run_id: "run-1",
    conversation_id: "conv-1",
    name: "fs_write",
    arguments_json: "{}",
    proposal_preview: "--- a\n+++ b\n@@\n-one\n+two\n",
    status: "proposed",
    result_preview: "",
    error: "",
    latency_ms: 0,
    artifacts: [],
    approved_by_mode: "",
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

/** The assistant turn a tool card hangs off: cards render inside the message
 *  whose `run_id` they share, so a bare `agentCalls` array renders nothing. */
const ANSWER: Message = {
  id: "m-answer",
  run_id: "run-1",
  role: "assistant",
  content: "here is the answer",
  citations: [],
  citation_report: null,
  sender_id: "",
  sender_name: "",
  created_at: "2026-09-20T00:00:00Z",
};

function chat(agentCalls: AgentToolCall[]) {
  return render(
    createElement(ChatView, { ...BASE, messages: [ANSWER], agentCalls }),
  );
}

describe("the gate note on an approval card", () => {
  const NOTE = /This turn read a result from a connected MCP server/;

  it("renders on a proposed call that carries a reason", () => {
    chat([call({ gate_reason: "taint:mcp_result:write" })]);
    expect(screen.getByText(NOTE)).toBeTruthy();
  });

  it("renders nothing on an identical call without one", () => {
    chat([call()]);
    expect(screen.queryByText(NOTE)).toBeNull();
  });

  it("goes quiet once the call has been decided", () => {
    // "It is waiting for you" is present tense. A settled call's reason lives
    // in the trail; repeating it on a card nobody is deciding is noise.
    chat([call({ status: "succeeded", gate_reason: "taint:mcp_result:write" })]);
    expect(screen.queryByText(NOTE)).toBeNull();
  });

  it("sits above the preview it is the reason for", () => {
    const { container } = chat([call({ gate_reason: "taint:mcp_result:write" })]);
    const note = container.querySelector(".tool-gate-note");
    const preview = container.querySelector(".proposal-diff, .tool-card .diff-lines");
    expect(note).toBeTruthy();
    if (preview) {
      // Node.DOCUMENT_POSITION_FOLLOWING — the preview comes after the note.
      expect(note!.compareDocumentPosition(preview) & 4).toBeTruthy();
    }
  });
});
