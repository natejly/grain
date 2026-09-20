import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Conversation } from "@workspace/api-client";

/**
 * A deleted thread must not come back because a slower request said it was there.
 *
 * The rail applies whole-list snapshots from `listConversations()`, and a
 * snapshot is only true as of the moment it was *requested*. When a run settles
 * the shell re-reads the list; if the user deletes a thread while that read is
 * in flight, the reply still contains the row — the DELETE had not committed
 * when the server answered it — and lands after the optimistic removal.
 *
 * Observed in CI rather than imagined, from the trace of a "flaky"
 * budget.spec.ts:
 *
 *   53.022  GET    /api/conversations/<id>   200   (issued BEFORE the delete)
 *   53.025  DELETE /api/conversations/<id>   204   (commits at 53.051)
 *   53.037  GET    /api/conversations        200   (returns the row, lands 53.050)
 *   06:00.5 GET    /api/conversations              (next refresh, 7.5s later)
 *
 * The row was therefore back for about seven seconds — long enough to read as
 * a delete that did not work, and to delete it a second time against something
 * already gone. The end state was correct, which is precisely why it presented
 * as flakiness instead of as a bug.
 *
 * These tests hold the refresh open on purpose, so the interleaving is the
 * thing under test rather than something the scheduler has to be lucky about.
 */

const ALPHA = { id: "c-alpha", title: "Alpha" };
const BETA = { id: "c-beta", title: "Beta" };

function conversation(seed: { id: string; title: string }): Conversation {
  return {
    subject_kind: "",
    subject_id: "",
    approval_mode: "auto_writes",
    shared: false,
    incognito: false,
    owned: true,
    can_share: true,
    space_id: "",
    default_agent_id: "",
    default_model: "",
    default_effort: "",
    created_at: "2026-08-27T00:00:00Z",
    updated_at: "2026-08-27T00:00:00Z",
    ...seed,
  };
}

/** A promise whose resolution this test controls, so a request can be held. */
function deferred<T>() {
  let settle: (value: T) => void = () => {};
  const promise = new Promise<T>((resolve) => {
    settle = resolve;
  });
  return { promise, settle };
}

const listConversations = vi.fn();
const deleteConversation = vi.fn();
const createConversation = vi.fn();
const listMemory = vi.fn();
const deleteMemory = vi.fn();
const updateMemory = vi.fn();

vi.mock("../components/use-coworking", () => ({
  useCoworking: () => ({
    runs: [],
    presences: [],
    othersOn: () => [],
    report: () => {},
    reportPointer: () => {},
    leave: () => {},
  }),
}));

vi.mock("../components/api", () => {
  // Every endpoint the workspace load touches, answered with an empty shape.
  // Only the two this test is about carry behaviour.
  const empty = new Proxy(
    {},
    {
      get(_target, name: string) {
        if (name === "listConversations") return listConversations;
        if (name === "deleteConversation") return deleteConversation;
        if (name === "createConversation") return createConversation;
        if (name === "listMemory") return listMemory;
        if (name === "deleteMemory") return deleteMemory;
        if (name === "updateMemory") return updateMemory;
        if (name === "bootstrap") {
          // Only the fields this hook dereferences without a guard: `identity`
          // and `model_provider` are reached through `bootstrap?.x.y`, which
          // short-circuits on a null bootstrap but not on a missing branch.
          return () =>
            Promise.resolve({
              identity: { user_id: "u-1", workspace_id: "w-1" },
              default_agent_id: "",
              feature_flags: {},
              model_provider: {
                provider: "scripted",
                configured: false,
                model: "scripted-double",
                selectable_models: [],
                reasoning_efforts: [],
                default_effort: "",
              },
              screen: { enabled: false },
              safe_mode: false,
            });
        }
        if (name === "getInbox") {
          return () => Promise.resolve({ approvals: [], mentions: [], comments: [] });
        }
        return () => Promise.resolve([]);
      },
    },
  );
  return { api: empty };
});

describe("the rail and a snapshot that was overtaken", () => {
  beforeEach(() => {
    listConversations.mockReset();
    deleteConversation.mockReset();
    deleteConversation.mockResolvedValue(undefined);
    createConversation.mockReset();
    createConversation.mockResolvedValue(conversation({ id: "c-fresh", title: "Fresh" }));
    vi.spyOn(window, "confirm").mockReturnValue(true);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  async function mounted() {
    const { useWorkspace } = await import("../components/use-workspace");
    const view = renderHook(() => useWorkspace());
    await waitFor(() => expect(view.result.current.conversations).toHaveLength(2));
    return view;
  }

  it("drops a refresh that was requested before the delete", async () => {
    const held = deferred<unknown[]>();
    listConversations
      // The initial workspace load.
      .mockResolvedValueOnce([conversation(ALPHA), conversation(BETA)])
      // The settle-triggered refresh, held open across the delete.
      .mockReturnValueOnce(held.promise);

    const view = await mounted();

    // In flight, and deliberately not awaited: this is the request that was
    // issued before the delete and answers after it.
    let refreshing: Promise<void> = Promise.resolve();
    act(() => {
      refreshing = view.result.current.refreshConversations();
    });

    await act(async () => {
      await view.result.current.removeConversation(conversation(ALPHA));
    });
    expect(view.result.current.conversations.map((row) => row.id)).toEqual([BETA.id]);

    // The server answers the older question: Alpha still exists.
    await act(async () => {
      held.settle([conversation(ALPHA), conversation(BETA)]);
      await refreshing;
    });

    expect(
      view.result.current.conversations.map((row) => row.id),
      "a stale snapshot resurrected the deleted thread",
    ).toEqual([BETA.id]);
  });

  it("keeps the history when a thread is made during the first load", async () => {
    // The mirror of the delete case, and the reason the guard cannot simply
    // drop a snapshot it does not trust. `loadWorkspace` runs once from a
    // mount effect and nothing on a timer re-reads conversations, so a rail
    // left holding only the locally-made thread stays that way until the user
    // happens to finish a run — not for seven seconds, indefinitely.
    const held = deferred<unknown[]>();
    listConversations.mockReturnValueOnce(held.promise);

    const { useWorkspace } = await import("../components/use-workspace");
    const view = renderHook(() => useWorkspace());

    // "New thread", clicked while the first load is still in flight.
    await act(async () => {
      await view.result.current.newConversation();
    });

    await act(async () => {
      held.settle([conversation(ALPHA), conversation(BETA)]);
      await Promise.resolve();
    });

    await waitFor(() =>
      expect(
        view.result.current.conversations.map((row: Conversation) => row.id),
        "the load dropped the history it had just fetched",
      ).toEqual(["c-fresh", ALPHA.id, BETA.id]),
    );
  });

  it("still applies a refresh that nothing overtook", async () => {
    listConversations
      .mockResolvedValueOnce([conversation(ALPHA), conversation(BETA)])
      // Somebody else's new thread: no local change raced this, so it lands.
      .mockResolvedValueOnce([
        conversation({ id: "c-gamma", title: "Gamma" }),
        conversation(ALPHA),
        conversation(BETA),
      ]);

    const view = await mounted();
    await act(async () => {
      await view.result.current.refreshConversations();
    });

    expect(
      view.result.current.conversations.map((row) => row.id),
      "the guard must not make the rail stop updating",
    ).toEqual(["c-gamma", ALPHA.id, BETA.id]);
  });
});

// --- The same race, on the memory shelf --------------------------------------
//
// refreshMemories fires for every member's settled run (the memory.updated
// SSE), so a slow reply can straddle a local Forget or an inline edit exactly
// the way a conversations snapshot straddles a delete. Same epoch guard, same
// tests: the overtaken snapshot is dropped, the innocent one still lands.

function memoryRow(seed: { id: string; content: string }) {
  return {
    conversation_id: null,
    space_id: "",
    kind: "fact",
    entity_names: [],
    message_ids: [],
    importance: 1,
    shared: false,
    created_at: "2026-08-27T00:00:00Z",
    updated_at: "2026-08-27T00:00:00Z",
    ...seed,
  };
}

const MEM_A = memoryRow({ id: "m-a", content: "Deploys go out on Fridays." });
const MEM_B = memoryRow({ id: "m-b", content: "The API deploys on Railway." });

describe("the memory shelf and a snapshot that was overtaken", () => {
  beforeEach(() => {
    listConversations.mockReset().mockResolvedValue([conversation(ALPHA)]);
    listMemory.mockReset();
    deleteMemory.mockReset().mockResolvedValue(undefined);
    updateMemory.mockReset();
    vi.spyOn(window, "confirm").mockReturnValue(true);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  async function mountedWithMemories() {
    const { useWorkspace } = await import("../components/use-workspace");
    const view = renderHook(() => useWorkspace());
    await waitFor(() => expect(view.result.current.memories).toHaveLength(2));
    return view;
  }

  it("does not resurrect a just-forgotten memory from a slower refresh", async () => {
    const held = deferred<unknown[]>();
    listMemory
      // The initial workspace load.
      .mockResolvedValueOnce([MEM_A, MEM_B])
      // The event-triggered refresh, held open across the forget.
      .mockReturnValueOnce(held.promise);

    const view = await mountedWithMemories();

    let refreshing: Promise<void> = Promise.resolve();
    act(() => {
      refreshing = view.result.current.refreshMemories();
    });

    await act(async () => {
      await view.result.current.forgetMemory(MEM_A as never);
    });
    expect(view.result.current.memories.map((row: { id: string }) => row.id)).toEqual([
      MEM_B.id,
    ]);

    // The server answers the older question: the forgotten row still exists.
    await act(async () => {
      held.settle([MEM_A, MEM_B]);
      await refreshing;
    });

    expect(
      view.result.current.memories.map((row: { id: string }) => row.id),
      "a stale snapshot resurrected the forgotten memory",
    ).toEqual([MEM_B.id]);
  });

  it("does not revert a just-saved inline edit from a slower refresh", async () => {
    const held = deferred<unknown[]>();
    listMemory.mockResolvedValueOnce([MEM_A, MEM_B]).mockReturnValueOnce(held.promise);
    const edited = { ...MEM_B, content: "The API deploys on Render." };
    updateMemory.mockResolvedValue(edited);

    const view = await mountedWithMemories();

    let refreshing: Promise<void> = Promise.resolve();
    act(() => {
      refreshing = view.result.current.refreshMemories();
    });

    await act(async () => {
      await view.result.current.editMemory(MEM_B as never, edited.content);
    });

    await act(async () => {
      held.settle([MEM_A, MEM_B]);
      await refreshing;
    });

    const row = view.result.current.memories.find(
      (item: { id: string }) => item.id === MEM_B.id,
    ) as { content: string };
    expect(
      row.content,
      "a stale snapshot reverted an edit the server had already taken",
    ).toBe("The API deploys on Render.");
  });

  it("still applies a refresh that nothing overtook", async () => {
    listMemory
      .mockResolvedValueOnce([MEM_A, MEM_B])
      // A teammate's run taught it something new; no local change raced it.
      .mockResolvedValueOnce([
        memoryRow({ id: "m-c", content: "Standup moved to 9:30." }),
        MEM_A,
        MEM_B,
      ]);

    const view = await mountedWithMemories();
    await act(async () => {
      await view.result.current.refreshMemories();
    });

    expect(
      view.result.current.memories.map((row: { id: string }) => row.id),
      "the guard must not make the shelf stop updating",
    ).toEqual(["m-c", MEM_A.id, MEM_B.id]);
  });
});
