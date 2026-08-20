import { describe, expect, it } from "vitest";
import {
  CHAT_PANES_KEY,
  MAX_EXTRA_PANES,
  addPane,
  parseStoredPanes,
  prunePanes,
  refocusAfterClose,
  removePane,
  serializeStoredPanes,
  type ChatPane,
} from "../components/chat-panes";

/**
 * The multi-pane split's layout rules, tested without a DOM. Everything that
 * decides whether a chat pane opens, closes, keeps focus, survives a deleted
 * conversation, or a reload lives in these pure helpers; the hook only wires
 * them to React state. If one of these transitions is wrong, two people running
 * concurrent chats see the wrong panes.
 */

function pane(id: string, conversationId: string): ChatPane {
  return { id, conversationId };
}

describe("opening a pane", () => {
  it("appends an independent entry with the id it is given", () => {
    const next = addPane([], "conv-a", null, "pane-1");
    expect(next).toEqual([pane("pane-1", "conv-a")]);
  });

  it("keeps existing panes and their order when a new one opens", () => {
    const first = addPane([], "conv-a", null, "pane-1");
    const second = addPane(first, "conv-b", null, "pane-2");
    expect(second).toEqual([pane("pane-1", "conv-a"), pane("pane-2", "conv-b")]);
    // The append must not mutate the list the caller handed in — React state.
    expect(first).toEqual([pane("pane-1", "conv-a")]);
  });

  it("lets the same conversation open in two panes, keyed by distinct ids", () => {
    // A user comparing one thread against itself is allowed; the client-side id,
    // not the conversation id, is what keeps the two React nodes apart.
    const twice = addPane(addPane([], "conv-a", null, "pane-1"), "conv-a", null, "pane-2");
    expect(twice.map((p) => p.id)).toEqual(["pane-1", "pane-2"]);
    expect(twice.every((p) => p.conversationId === "conv-a")).toBe(true);
  });

  it("ignores an open of the conversation already shown as the primary", () => {
    // Pane 0 is implicit; a second copy of what it already shows is not a split.
    const panes = [pane("pane-1", "conv-a")];
    expect(addPane(panes, "conv-primary", "conv-primary", "pane-2")).toBe(panes);
  });
});

describe("the pane cap", () => {
  const full: ChatPane[] = Array.from({ length: MAX_EXTRA_PANES }, (_, i) =>
    pane(`pane-${i}`, `conv-${i}`),
  );

  it("holds at the cap, returning the same list untouched", () => {
    expect(full).toHaveLength(MAX_EXTRA_PANES);
    const rejected = addPane(full, "conv-extra", null, "pane-x");
    expect(rejected).toBe(full);
    expect(rejected).toHaveLength(MAX_EXTRA_PANES);
  });

  it("admits the pane that exactly fills the last slot", () => {
    const oneShort = full.slice(0, MAX_EXTRA_PANES - 1);
    const filled = addPane(oneShort, "conv-last", null, "pane-last");
    expect(filled).toHaveLength(MAX_EXTRA_PANES);
    expect(filled[filled.length - 1]).toEqual(pane("pane-last", "conv-last"));
  });
});

describe("closing a pane", () => {
  const panes = [pane("pane-1", "conv-a"), pane("pane-2", "conv-b")];

  it("removes just the closed pane and keeps the rest", () => {
    expect(removePane(panes, "pane-1")).toEqual([pane("pane-2", "conv-b")]);
  });

  it("closing the last extra pane returns the empty single-pane layout", () => {
    // An empty list is the shell exactly as it has always been — the guaranteed
    // no-regression path — so closing everything must land there, not on junk.
    const emptied = removePane([pane("pane-1", "conv-a")], "pane-1");
    expect(emptied).toEqual([]);
  });

  it("is a no-op for a pane id that is not open", () => {
    expect(removePane(panes, "gone")).toEqual(panes);
  });

  it("re-focuses the primary when the closed pane held focus", () => {
    expect(refocusAfterClose("pane-1", "pane-1")).toBeNull();
  });

  it("leaves focus alone when a different pane closes", () => {
    expect(refocusAfterClose("pane-2", "pane-1")).toBe("pane-2");
  });

  it("stays on the primary when nothing was focused", () => {
    expect(refocusAfterClose(null, "pane-1")).toBeNull();
  });
});

describe("pruning panes whose conversation is gone", () => {
  it("drops a pane whose conversation no longer exists", () => {
    const panes = [pane("pane-1", "conv-a"), pane("pane-2", "conv-b")];
    expect(prunePanes(panes, new Set(["conv-a"]))).toEqual([pane("pane-1", "conv-a")]);
  });

  it("returns the identical list when every conversation still exists", () => {
    // Identity is load-bearing: the React effect that prunes on every
    // conversations change must be able to skip a write when nothing moved.
    const panes = [pane("pane-1", "conv-a")];
    expect(prunePanes(panes, new Set(["conv-a", "conv-b"]))).toBe(panes);
  });

  it("empties the split when the last surviving conversation is deleted", () => {
    const panes = [pane("pane-1", "conv-a")];
    expect(prunePanes(panes, new Set())).toEqual([]);
  });
});

describe("persisting the layout", () => {
  it("uses the grain-namespaced storage key", () => {
    expect(CHAT_PANES_KEY).toBe("grain.chat-panes");
  });

  it("round-trips a layout through serialize and parse", () => {
    const layout = [pane("pane-1", "conv-a"), pane("pane-2", "conv-b")];
    expect(parseStoredPanes(serializeStoredPanes(layout))).toEqual(layout);
  });

  it("round-trips an empty split", () => {
    expect(parseStoredPanes(serializeStoredPanes([]))).toEqual([]);
  });

  it("reads a missing key as an empty split", () => {
    expect(parseStoredPanes(null)).toEqual([]);
  });

  it("survives malformed JSON rather than throwing", () => {
    // The value is one hand-editable localStorage key; a parse crash here would
    // take the whole shell down on load.
    expect(parseStoredPanes("{not json")).toEqual([]);
  });

  it("ignores a stored value that is not an array", () => {
    expect(parseStoredPanes('{"id":"pane-1"}')).toEqual([]);
  });

  it("drops rows missing an id or conversation id", () => {
    const raw = JSON.stringify([
      { id: "pane-1", conversationId: "conv-a" },
      { id: "pane-2" },
      { conversationId: "conv-c" },
      null,
      "nope",
    ]);
    expect(parseStoredPanes(raw)).toEqual([pane("pane-1", "conv-a")]);
  });

  it("holds a hand-edited over-cap store to the cap", () => {
    const raw = JSON.stringify(
      Array.from({ length: MAX_EXTRA_PANES + 2 }, (_, i) => pane(`pane-${i}`, `conv-${i}`)),
    );
    expect(parseStoredPanes(raw)).toHaveLength(MAX_EXTRA_PANES);
  });

  it("keeps only the first row for a duplicated id", () => {
    const raw = JSON.stringify([
      pane("pane-1", "conv-a"),
      pane("pane-1", "conv-b"),
      pane("pane-2", "conv-c"),
    ]);
    expect(parseStoredPanes(raw)).toEqual([pane("pane-1", "conv-a"), pane("pane-2", "conv-c")]);
  });
});
