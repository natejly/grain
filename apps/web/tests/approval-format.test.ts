import type { AgentToolCall } from "@workspace/api-client";
import { describe, expect, it } from "vitest";
import {
  APPROVAL_MODES,
  autoApprovedCalls,
  describeMode,
  isBypass,
  summariseAutoApproved,
} from "../components/views/approval-format";

/**
 * The bypass is the only thing in the product that removes an approval park, so
 * the two claims this module makes have to hold under a mistake:
 *
 *  - an *unrecognised* mode reads as the strict one, never as "no approvals
 *    needed" — a conversation stored with a value this build has since dropped
 *    must not render as a thread that asks for nothing;
 *  - the trail is what the server said the mode decided, never an inference
 *    from "the mode was on and this was a write". The server deliberately does
 *    not credit the bypass for a call a standing policy already allowed, and
 *    guessing here would put that claim back.
 */
function call(id: string, name: string, approvedByMode: string, conversationId = "conv-1") {
  return {
    id,
    run_id: "run-1",
    conversation_id: conversationId,
    name,
    arguments_json: "{}",
    proposal_preview: "",
    status: "succeeded",
    result_preview: "",
    error: "",
    latency_ms: 0,
    artifacts: [],
    approved_by_mode: approvedByMode,
    created_at: "2026-01-01T00:00:00Z",
  } as AgentToolCall;
}

describe("approval modes", () => {
  it("offers exactly one mode that lets a write through unreviewed", () => {
    expect(APPROVAL_MODES.filter((mode) => mode.bypass).map((mode) => mode.mode)).toEqual([
      "auto_writes",
    ]);
  });

  it("lands an unknown mode on the strict answer", () => {
    expect(describeMode("something_new").mode).toBe("ask_writes");
    expect(isBypass("something_new")).toBe(false);
    expect(isBypass("")).toBe(false);
    expect(isBypass("auto_writes")).toBe(true);
  });

  it("says what will happen rather than naming the setting", () => {
    // Every string on this surface is read later, by someone who does not
    // remember making the choice.
    for (const mode of APPROVAL_MODES) {
      expect(mode.detail.length).toBeGreaterThan(20);
    }
    expect(describeMode("auto_writes").detail).toMatch(/Denied tools stay denied/);
  });
});

describe("the trail a bypassed thread carries", () => {
  it("lists only what the server attributed to a mode, in this thread", () => {
    const calls = [
      call("1", "create_document", "auto_writes"),
      // A standing `allow` decided this one; the bypass had no part in it.
      call("2", "search_sources", ""),
      // Another thread's bypass is not this thread's business.
      call("3", "edit_document", "auto_writes", "conv-2"),
    ];
    expect(autoApprovedCalls(calls, "conv-1").map((item) => item.id)).toEqual(["1"]);
  });

  it("is empty when there is no thread open", () => {
    expect(autoApprovedCalls([call("1", "create_document", "auto_writes")], null)).toEqual([]);
  });

  it("summarises repeats by tool rather than by call", () => {
    expect(summariseAutoApproved([])).toBe("Nothing yet");
    expect(summariseAutoApproved([call("1", "add_todo", "auto_writes")])).toBe("add_todo");
    expect(
      summariseAutoApproved([
        call("1", "add_todo", "auto_writes"),
        call("2", "add_todo", "auto_writes"),
      ]),
    ).toBe("add_todo");
    expect(
      summariseAutoApproved([
        call("1", "add_todo", "auto_writes"),
        call("2", "create_document", "auto_writes"),
      ]),
    ).toBe("add_todo and 1 more");
  });
});
