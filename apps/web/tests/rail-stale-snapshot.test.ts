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
