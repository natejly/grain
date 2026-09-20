import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Conversation } from "@workspace/api-client";

/**
 * The goodbye a thread's presence surface owes on the way out.
 *
 * The composer's typing effect beats `conversation:<id>` per keystroke and
 * downgrades on a 3s timer — but its cleanup runs per keystroke too, so it
 * can only ever clear the timer. Switching threads inside that 3s window
 * used to cancel the pending {typing:false} with nothing ever addressing the
 * old surface again, and the coworking re-beat loop then re-sent the
 * stranded {typing:true} forever: teammates saw "X is typing…" on the
 * abandoned thread until the tab closed.
 *
 * The fix is a `leave` keyed on the surface alone — the documents pattern —
 * which bypasses the throttle, cancels queued beats and drops the surface
 * from the re-beat map. These tests pin that the leave fires on a thread
 * switch and on unmount, and always for the surface being LEFT.
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

const report = vi.fn();
const leave = vi.fn();

vi.mock("../components/use-coworking", () => ({
  useCoworking: () => ({
    runs: [],
    presences: [],
    othersOn: () => [],
    report,
    reportPointer: () => {},
    leave,
  }),
}));

vi.mock("../components/api", () => {
  const empty = new Proxy(
    {},
    {
      get(_target, name: string) {
        if (name === "listConversations") {
          return () => Promise.resolve([conversation(ALPHA), conversation(BETA)]);
        }
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

describe("the conversation surface's goodbye", () => {
  beforeEach(() => {
    report.mockClear();
    leave.mockClear();
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    window.history.replaceState({}, "", "/");
  });

  async function mounted() {
    const { useWorkspace } = await import("../components/use-workspace");
    const view = renderHook(() => useWorkspace());
    await waitFor(() => expect(view.result.current.activeConversation).toBe(ALPHA.id));
    return view;
  }

  it("leaves the OLD thread's surface when the focus moves to another", async () => {
    const view = await mounted();
    expect(leave).not.toHaveBeenCalled();

    await act(async () => {
      await view.result.current.selectConversation(BETA.id);
    });

    // The abandoned surface got its goodbye — the DELETE-now, drop-the-beat
    // path, not a throttled report that a queued {typing:true} could outrun.
    expect(leave).toHaveBeenCalledWith(`conversation:${ALPHA.id}`);
    // And only the abandoned one: leaving the surface just arrived on would
    // blink the user out of the thread they are actually in.
    expect(leave).not.toHaveBeenCalledWith(`conversation:${BETA.id}`);
  });

  it("leaves the active surface when the shell unmounts", async () => {
    const view = await mounted();

    view.unmount();

    expect(leave).toHaveBeenCalledWith(`conversation:${ALPHA.id}`);
  });
});
