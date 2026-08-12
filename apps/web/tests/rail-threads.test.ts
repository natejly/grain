import { describe, expect, it } from "vitest";
import type { Conversation, Message } from "@workspace/api-client";
import {
  groupThreads,
  senderInitial,
  senderLabel,
  shareControl,
} from "../components/views/shared";

/**
 * The chat rail's multiplayer logic, tested without a DOM. Three pure helpers
 * decide the rail's whole behaviour once a conversation can be personal or
 * shared: how the one server list splits into Personal and Shared groups,
 * whether the share/unshare toggle may render on a row, and how a message is
 * attributed on a shared thread. The JSX only wires these to the tree, so if
 * one transition here is wrong the rail hides a shared thread, offers a control
 * that 403s, or mislabels a teammate's turn as "You".
 *
 * The load-bearing property under all of it: sharing only ever changes
 * visibility WITHIN the workspace. These helpers never see a workspace id
 * because the server has already filtered to the caller's workspace; they must
 * never invent cross-workspace behaviour, only read the `shared`/`can_share`
 * flags the server resolved.
 */

function conversation(overrides: Partial<Conversation> = {}): Conversation {
  return {
    id: "conv-1",
    title: "Thread",
    document_id: "",
    approval_mode: "ask_writes",
    shared: false,
    owned: true,
    can_share: true,
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:00Z",
    ...overrides,
  };
}

function message(overrides: Partial<Message> = {}): Message {
  return {
    id: "msg-1",
    run_id: "run-1",
    role: "user",
    content: "hi",
    citations: [],
    citation_report: null,
    sender_id: "",
    sender_name: "",
    created_at: "2026-08-01T00:00:00Z",
    ...overrides,
  };
}

describe("splitting the rail into Personal and Shared", () => {
  it("files a thread by its shared flag, not by ownership", () => {
    // The server may return a shared thread the caller does not own; it still
    // belongs under Shared. Re-deriving the group from `owned` would hide it.
    const list = [
      conversation({ id: "a", shared: false, owned: true }),
      conversation({ id: "b", shared: true, owned: false }),
      conversation({ id: "c", shared: true, owned: true }),
      conversation({ id: "d", shared: false, owned: true }),
    ];
    const { personal, shared } = groupThreads(list);
    expect(personal.map((c) => c.id)).toEqual(["a", "d"]);
    expect(shared.map((c) => c.id)).toEqual(["b", "c"]);
  });

  it("preserves the server's order within each group", () => {
    // The rail shows recency order; the split must not reshuffle it.
    const list = [
      conversation({ id: "s1", shared: true }),
      conversation({ id: "p1", shared: false }),
      conversation({ id: "s2", shared: true }),
      conversation({ id: "p2", shared: false }),
    ];
    const { personal, shared } = groupThreads(list);
    expect(shared.map((c) => c.id)).toEqual(["s1", "s2"]);
    expect(personal.map((c) => c.id)).toEqual(["p1", "p2"]);
  });

  it("yields two empty groups for no conversations", () => {
    expect(groupThreads([])).toEqual({ personal: [], shared: [] });
  });

  it("puts every thread in one group when they are all the same kind", () => {
    const shared = [conversation({ id: "a", shared: true }), conversation({ id: "b", shared: true })];
    const grouped = groupThreads(shared);
    expect(grouped.shared).toHaveLength(2);
    expect(grouped.personal).toEqual([]);
  });
});

describe("whether the share toggle may render", () => {
  it("renders on the open thread the caller may re-scope", () => {
    const conv = conversation({ id: "open", can_share: true });
    expect(shareControl(conv, "open")).not.toBeNull();
  });

  it("stays hidden on a thread that is not the open one", () => {
    // The toggle rides only the open thread — one control, on the row in focus.
    const conv = conversation({ id: "other", can_share: true });
    expect(shareControl(conv, "open")).toBeNull();
  });

  it("stays hidden when nothing is open", () => {
    expect(shareControl(conversation({ id: "x" }), null)).toBeNull();
  });

  it("stays hidden for a member who may not re-scope it", () => {
    // A control that 403s on click is worse than no control: a member without
    // can_share must never see the button even on the open thread.
    const conv = conversation({ id: "open", can_share: false });
    expect(shareControl(conv, "open")).toBeNull();
  });
});

describe("the share toggle's labels", () => {
  it("offers to share a personal thread", () => {
    const control = shareControl(conversation({ id: "open", title: "Notes", shared: false }), "open");
    expect(control).toEqual({
      shared: false,
      title: "Share with the workspace",
      ariaLabel: "Share Notes with the workspace",
      pressed: false,
    });
  });

  it("offers to make a shared thread personal again", () => {
    const control = shareControl(conversation({ id: "open", title: "Notes", shared: true }), "open");
    expect(control).toEqual({
      shared: true,
      title: "Shared with the workspace — make personal",
      ariaLabel: "Make Notes personal",
      pressed: true,
    });
  });

  it("reports pressed state so the toggle reads as on/off to assistive tech", () => {
    expect(shareControl(conversation({ id: "o", shared: true }), "o")?.pressed).toBe(true);
    expect(shareControl(conversation({ id: "o", shared: false }), "o")?.pressed).toBe(false);
  });

  it("names the thread it acts on in the aria-label", () => {
    const control = shareControl(conversation({ id: "o", title: "Q3 planning", shared: false }), "o");
    expect(control?.ariaLabel).toContain("Q3 planning");
  });
});

describe("attributing a message's name", () => {
  it("names the sender on a shared thread so a teammate's turn is not 'You'", () => {
    const label = senderLabel(message({ role: "user", sender_name: "Ada" }), true);
    expect(label).toBe("Ada");
  });

  it("says 'You' for a user turn on a personal thread, where the name is noise", () => {
    expect(senderLabel(message({ role: "user", sender_name: "Ada" }), false)).toBe("You");
  });

  it("falls back to 'You' for a shared message that predates the sender column", () => {
    // sender_name is "" for rows written before attribution existed; a blank
    // name must never render as an empty author line.
    expect(senderLabel(message({ role: "user", sender_name: "" }), true)).toBe("You");
  });

  it("labels an assistant turn 'Assistant' regardless of thread scope", () => {
    expect(senderLabel(message({ role: "assistant", sender_name: "Ada" }), true)).toBe("Assistant");
    expect(senderLabel(message({ role: "assistant" }), false)).toBe("Assistant");
  });

  it("treats a tool row as non-user, so it is 'Assistant' too", () => {
    expect(senderLabel(message({ role: "tool", sender_name: "Ada" }), true)).toBe("Assistant");
  });
});

describe("the message's avatar initial", () => {
  it("uses the sender's initial on a shared thread, so two members differ", () => {
    expect(senderInitial(message({ role: "user", sender_name: "ada" }), true)).toBe("A");
    expect(senderInitial(message({ role: "user", sender_name: "ben" }), true)).toBe("B");
  });

  it("falls back to 'U' for a user turn on a personal thread", () => {
    expect(senderInitial(message({ role: "user", sender_name: "Ada" }), false)).toBe("U");
  });

  it("falls back to 'U' for an unattributed shared message", () => {
    expect(senderInitial(message({ role: "user", sender_name: "" }), true)).toBe("U");
  });

  it("always shows 'A' for a non-user row", () => {
    expect(senderInitial(message({ role: "assistant", sender_name: "Ada" }), true)).toBe("A");
    expect(senderInitial(message({ role: "tool" }), true)).toBe("A");
  });
});
