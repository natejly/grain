import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Conversation } from "@workspace/api-client";

/**
 * The "Memory updated" toast's two fairness rules, downstream of the
 * memory-signal plumbing:
 *
 *   - "your run" means any run this tab started — the rail, an extra split
 *     pane, the panel beside a document. The ids are registered at the one
 *     seam every client-started run passes through (`followRun`), not by
 *     wrapping the primary chat's setter, which is how split-pane runs used
 *     to lose their toast;
 *   - a background SSE event must not evict a sticky notice: an undo's
 *     skipped half is a thing to act on, and "Memory updated" is a transient
 *     line the refreshed page repeats anyway.
 */

const ALPHA = { id: "c-alpha", title: "Alpha" };

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

// The coworking hook, stubbed to hand this test the durable-event callback
// the workspace registers — firing it IS the SSE frame arriving.
let deliverEvent: ((eventType: string, data?: unknown) => void) | undefined;

vi.mock("../components/use-coworking", () => ({
  useCoworking: (_selfId: string, onEvent?: (e: string, d?: unknown) => void) => {
    deliverEvent = onEvent;
    return {
      runs: [],
      presences: [],
      othersOn: () => [],
      report: () => {},
      reportPointer: () => {},
      leave: () => {},
    };
  },
}));

vi.mock("../components/api", () => {
  // Every endpoint the workspace load touches, answered with an empty shape.
  const empty = new Proxy(
    {},
    {
      get(_target, name: string) {
        if (name === "listConversations") {
          return () => Promise.resolve([conversation(ALPHA)]);
        }
        if (name === "streamRun") {
          // A run that settles immediately: followRun's whole journey, so the
          // register-at-start seam is exercised the way a real run exercises it.
          return async function* () {
            yield { event: "run.completed", data: {} };
          };
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

import { createThreadHandlers } from "../components/handlers/thread";
import { isOwnRun, registerOwnRun, resetOwnRuns } from "../components/own-runs";

describe("own-run registration and the Memory updated toast", () => {
  beforeEach(() => {
    resetOwnRuns();
    deliverEvent = undefined;
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  async function mounted() {
    const { useWorkspace } = await import("../components/use-workspace");
    const view = renderHook(() => useWorkspace());
    await waitFor(() => expect(view.result.current.conversations).toHaveLength(1));
    await waitFor(() => expect(deliverEvent).toBeTruthy());
    return view;
  }

  it("followRun registers the run at the engine seam, whatever surface owns it", async () => {
    // A pane's own local setActiveRun — the exact shape use-conversation-thread
    // and use-subject-thread hold, which the old primary-only wrapper missed.
    const handlers = createThreadHandlers({
      messages: [],
      draft: "",
      activeConversation: "c-pane",
      activeRun: null,
      setError: vi.fn(),
      setMessages: vi.fn(),
      setAgentCalls: vi.fn(),
      setActiveRun: vi.fn(),
      setRunStatus: vi.fn(),
      setBudgetPark: vi.fn(),
      setDraft: vi.fn(),
      ensureConversation: async () => "c-pane",
      onRunSettled: vi.fn().mockResolvedValue(undefined),
      activeConversationRef: { current: "c-pane" },
    });

    await handlers.followRun("run-pane", "c-pane");

    expect(isOwnRun("run-pane")).toBe(true);
  });

  it("toasts for a registered run and stays quiet for a teammate's", async () => {
    const view = await mounted();
    registerOwnRun("run-own");

    act(() => {
      deliverEvent?.("memory.updated", { run_id: "run-theirs", count: 1 });
    });
    expect(view.result.current.notice).toBeNull();

    act(() => {
      deliverEvent?.("memory.updated", { run_id: "run-own", count: 1 });
    });
    await waitFor(() =>
      expect(view.result.current.notice?.text).toBe("Memory updated"),
    );
  });

  it("does not evict a sticky notice before it is read", async () => {
    const view = await mounted();
    registerOwnRun("run-own");

    act(() => {
      view.result.current.setNotice({
        text: "The run was undone; its summary could not be restored",
        at: Date.now(),
        sticky: true,
      });
    });

    act(() => {
      deliverEvent?.("memory.updated", { run_id: "run-own", count: 1 });
    });

    // The actionable line is still the one on screen; the transient toast
    // waited its turn (the Memory page the refresh feeds says the same thing).
    expect(view.result.current.notice?.sticky).toBe(true);
    expect(view.result.current.notice?.text).toBe(
      "The run was undone; its summary could not be restored",
    );
  });
});
