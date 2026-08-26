import type { AgentToolCall } from "@workspace/api-client";
import { describe, expect, it } from "vitest";
import {
  APPROVAL_MODES,
  actionableApprovals,
  assigneeName,
  autoApprovedCalls,
  describeMode,
  isBypass,
  partitionApprovals,
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
    assigned_to: "",
    created_at: "2026-01-01T00:00:00Z",
  } as AgentToolCall;
}

describe("approval modes", () => {
  it("marks exactly the modes that let a write through unreviewed", () => {
    // Two bypasses, both deliberate: auto_writes skips review entirely, and
    // guardian delegates it to a reviewer model. Both mean a write can run
    // without a person seeing it first, which is what the bypass banner and
    // the auto-approved trail exist to surface — so both must carry the flag.
    expect(APPROVAL_MODES.filter((mode) => mode.bypass).map((mode) => mode.mode)).toEqual([
      "auto_writes",
      "guardian",
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

describe("routing a parked approval", () => {
  const row = (id: string, assignedTo: string) => ({ id, assigned_to: assignedTo });

  it("splits the queue into yours, anyone's and theirs, keeping each group's order", () => {
    const rows = [
      row("1", "them"),
      row("2", ""),
      row("3", "me"),
      row("4", ""),
      row("5", "me"),
    ];
    const buckets = partitionApprovals(rows, "me");
    expect(buckets.mine.map((item) => item.id)).toEqual(["3", "5"]);
    expect(buckets.unassigned.map((item) => item.id)).toEqual(["2", "4"]);
    expect(buckets.others.map((item) => item.id)).toEqual(["1"]);
  });

  it("claims nothing as yours before the identity's first read lands", () => {
    // selfId "" must not make every unassigned row read as an assignment — and
    // an assigned row must fall to "others" rather than to a guess.
    const buckets = partitionApprovals([row("1", ""), row("2", "them")], "");
    expect(buckets.mine).toEqual([]);
    expect(buckets.unassigned.map((item) => item.id)).toEqual(["1"]);
    expect(buckets.others.map((item) => item.id)).toEqual(["2"]);
  });

  it("counts only what the caller can answer — theirs first, then anyone's", () => {
    // The rail badge and the waiting strip read this: a row routed to a
    // colleague is their wait, and a decide button on it would only 409.
    const rows = [row("1", "them"), row("2", ""), row("3", "me")];
    expect(actionableApprovals(rows, "me").map((item) => item.id)).toEqual(["3", "2"]);
  });

  it("names an assignee the member list no longer holds by id, never as Anyone", () => {
    const members = [{ user_id: "u1", name: "Ada", role: "member" }];
    expect(assigneeName("", members)).toBe("Anyone");
    expect(assigneeName("u1", members)).toBe("Ada");
    expect(assigneeName("gone-user", members)).toBe("gone-user");
  });
});
