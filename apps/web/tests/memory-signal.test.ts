import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

/**
 * The durable-event branch of the coworking stream, now carrying its payload.
 *
 * `memory.updated` is a workspace_event like any other, so it rides the
 * existing SSE with no stream change — but the toast has to tell "your run
 * taught it something" from a teammate's, and the only way it can is the
 * `run_id` in the frame's data. Two pins here: the payload is forwarded
 * beside the event name, and the runs/presence frames still never reach the
 * callback — they are snapshots for the strip, not events, and a consumer
 * that refreshed a list per presence beat would be a DDoS on its own server.
 */

vi.mock("../components/api", () => ({
  api: {
    coworkingActivity: () =>
      Promise.resolve({ runs: [], presences: [], last_event_sequence: 0 }),
    // One of each frame kind, then parked forever so the hook neither
    // redials nor re-delivers while the test asserts.
    streamCoworking: async function* () {
      yield { event: "runs", data: [] };
      yield { event: "presence", data: [] };
      yield { event: "memory.updated", data: { run_id: "run-1", count: 2 } };
      await new Promise(() => undefined);
    },
    heartbeatPresence: () => Promise.resolve(),
    leavePresence: () => Promise.resolve(),
  },
}));

import { useCoworking } from "../components/use-coworking";

afterEach(cleanup);

describe("the durable-event branch", () => {
  it("forwards the event name and its payload, and only for durable events", async () => {
    const seen: Array<[string, unknown]> = [];
    const view = renderHook(() =>
      useCoworking("me", (eventType, data) => {
        seen.push([eventType, data]);
      }),
    );
    await waitFor(() => expect(seen.length).toBeGreaterThan(0));
    // Exactly one delivery: the runs and presence frames took their own
    // branches (state for the strip), never the callback.
    expect(seen).toEqual([["memory.updated", { run_id: "run-1", count: 2 }]]);
    view.unmount();
  });
});
