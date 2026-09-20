import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Conversation } from "@workspace/api-client";

/**
 * The `?t=` deep-link chokepoint in use-workspace.
 *
 * Every thread-focus change flows through one wrapped setter, and every view
 * change through another; both schedule a microtask-coalesced sync that
 * projects `?view=` + `?t=` into the URL through `pushWorkspaceUrl`. These
 * tests pin the chokepoint's contract:
 *
 *   - focusing a thread writes `?t=` in ONE history entry, even though the
 *     select fires setActiveConversation and setView as a pair in one tick;
 *   - leaving chat drops `?t=`;
 *   - the initial page load pushes NOTHING (the auto-selected first thread is
 *     a default, not a navigation — no ghost entry under the back button);
 *   - a loaded or popstate'd `?t=` opens the thread only when the client
 *     actually knows it; an unknown id degrades silently to the default, the
 *     same fence philosophy `?view=garbage` gets from isView.
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

const listConversations = vi.fn();
const listMessages = vi.fn();

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
        if (name === "listMessages") return listMessages;
        if (name === "bootstrap") {
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

describe("the ?t= deep-link chokepoint", () => {
  beforeEach(() => {
    listConversations.mockReset();
    listConversations.mockResolvedValue([conversation(ALPHA), conversation(BETA)]);
    listMessages.mockReset();
    listMessages.mockResolvedValue([]);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    // jsdom's real history/location were mutated; reset to a clean path.
    window.history.replaceState({}, "", "/");
  });

  async function mounted() {
    const { useWorkspace } = await import("../components/use-workspace");
    const view = renderHook(() => useWorkspace());
    await waitFor(() => expect(view.result.current.conversations).toHaveLength(2));
    return view;
  }

  it("writes ?t= in one entry on focus, drops it on leaving chat, pushes nothing on load", async () => {
    const pushState = vi.spyOn(window.history, "pushState");
    const view = await mounted();
    await waitFor(() => expect(view.result.current.activeConversation).toBe(ALPHA.id));

    // The load auto-selected Alpha, and that is a default, not a navigation:
    // zero history entries so the back button still leaves the app.
    expect(pushState).not.toHaveBeenCalled();

    // Focusing a thread fires setActiveConversation AND setView("chat") in one
    // tick; the microtask coalesce must land them as ONE entry, not a ghost
    // entry per setter.
    await act(async () => {
      await view.result.current.selectConversation(BETA.id);
    });
    expect(pushState).toHaveBeenCalledTimes(1);
    expect(window.location.search).toBe("?view=chat&t=c-beta");

    // Leaving chat drops ?t=: a thread param under another view would
    // deep-link to a screen that never reads it.
    await act(async () => {
      view.result.current.setView("memory");
    });
    expect(window.location.search).toBe("?view=memory");
  });

  it("adopts a loaded ?t= naming a known thread, without writing history", async () => {
    window.history.replaceState({}, "", "/?view=chat&t=c-beta");
    const pushState = vi.spyOn(window.history, "pushState");
    const view = await mounted();

    await waitFor(() => expect(view.result.current.activeConversation).toBe(BETA.id));
    // The open flowed back through the wrapped setter, whose sync no-ops
    // against the URL the link already holds.
    expect(pushState).not.toHaveBeenCalled();
    expect(window.location.search).toBe("?view=chat&t=c-beta");
  });

  it("silently drops a loaded ?t= the client does not know", async () => {
    window.history.replaceState({}, "", "/?view=chat&t=c-ghost");
    const pushState = vi.spyOn(window.history, "pushState");
    const view = await mounted();

    // The default stands: the auto-selected first thread, no crash, no write.
    await waitFor(() => expect(view.result.current.activeConversation).toBe(ALPHA.id));
    expect(pushState).not.toHaveBeenCalled();
  });

  it("opens a known thread on popstate and ignores an unknown one", async () => {
    const view = await mounted();
    await waitFor(() => expect(view.result.current.activeConversation).toBe(ALPHA.id));

    // Back/forward to an entry holding Beta: the browser already moved the
    // URL, the handler adopts the thread, and the sync it schedules must
    // no-op rather than stack a fresh entry on top of the travelled one.
    const pushState = vi.spyOn(window.history, "pushState");
    await act(async () => {
      window.history.replaceState({}, "", "/?view=chat&t=c-beta");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    await waitFor(() => expect(view.result.current.activeConversation).toBe(BETA.id));
    expect(pushState).not.toHaveBeenCalled();

    // An entry naming a thread this client does not know — deleted since, or
    // someone else's — changes nothing.
    await act(async () => {
      window.history.replaceState({}, "", "/?view=chat&t=c-ghost");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(view.result.current.activeConversation).toBe(BETA.id);
    expect(pushState).not.toHaveBeenCalled();
  });
});
