// @vitest-environment jsdom
import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

/**
 * The pointer channel's clear, on surfaces the mouse never crossed.
 *
 * A cursor layer clears on unmount and on window blur — correct for a
 * surface that was pointed at, where the clear must beat the throttle so no
 * ghost cursor outlives the mouse. But the boards page mounts one layer PER
 * BOARD, and a clear that sends unconditionally would stamp its user onto
 * every board on the way out: N presence rows saying "here", from someone
 * who never moved their mouse at all. Nothing held means nothing to say.
 */

const heartbeatPresence = vi.fn().mockResolvedValue(undefined);
const leavePresence = vi.fn().mockResolvedValue(undefined);

vi.mock("../components/api", () => ({
  api: {
    heartbeatPresence: (...a: unknown[]) => heartbeatPresence(...a),
    leavePresence: (...a: unknown[]) => leavePresence(...a),
    // The stream never connects in this test; the channel under test is the
    // outbound one.
    coworkingActivity: () => new Promise(() => undefined),
    streamCoworking: vi.fn(),
  },
}));

import { useCoworking } from "../components/use-coworking";

afterEach(() => {
  vi.clearAllMocks();
});

describe("reportPointer(surface, null)", () => {
  it("sends nothing for a surface that never held a pointer", () => {
    const { result, unmount } = renderHook(() => useCoworking("u1"));

    act(() => {
      // What every cursor layer's unmount and blur handler sends.
      result.current.reportPointer("board:b1", null);
      result.current.reportPointer("board:b2", null);
    });

    expect(heartbeatPresence).not.toHaveBeenCalled();
    unmount();
  });

  it("says a full goodbye — past the throttle — for a surface only the pointer claimed", () => {
    const { result, unmount } = renderHook(() => useCoworking("u1"));

    act(() => {
      result.current.reportPointer("board:b1", { x: 0.5, y: 0.5 });
    });
    expect(heartbeatPresence).toHaveBeenCalledWith("board:b1", {
      pointer: { x: 0.5, y: 0.5 },
    });

    act(() => {
      result.current.reportPointer("board:b1", null);
    });
    // The goodbye went out NOW (the pointer throttle would have held a
    // second beat this close behind the first) — and it is a DELETE, not one
    // last empty heartbeat: a board layer never report()s, so the pointer
    // was the surface's only claim, and a beaten-but-empty row would ride
    // the re-beat loop forever, keeping the user "here" on every board the
    // mouse ever crossed.
    expect(leavePresence).toHaveBeenCalledWith("board:b1");
    expect(heartbeatPresence).toHaveBeenCalledTimes(1);
    unmount();
  });

  it("keeps a reported surface's presence when only the pointer clears", () => {
    // The documents contract: the view reports editing state on the same
    // surface the cursor layer points at. The mouse leaving the box must
    // clear the cursor — not delete the presence the view still owns.
    const { result, unmount } = renderHook(() => useCoworking("u1"));

    act(() => {
      result.current.report("document:d1", { typing: false });
      result.current.reportPointer("document:d1", { x: 0.2, y: 0.2 });
    });

    act(() => {
      result.current.reportPointer("document:d1", null);
    });
    // The clear re-sent the held state without the pointer; no DELETE.
    expect(leavePresence).not.toHaveBeenCalled();
    expect(heartbeatPresence).toHaveBeenLastCalledWith("document:d1", {
      typing: false,
    });
    unmount();
  });

  it("stops re-beating a surface once the pointer-only goodbye is said", () => {
    vi.useFakeTimers();
    try {
      const { result, unmount } = renderHook(() => useCoworking("u1"));

      act(() => {
        result.current.reportPointer("board:b1", { x: 0.5, y: 0.5 });
        result.current.reportPointer("board:b1", null);
      });
      heartbeatPresence.mockClear();

      // Two full re-beat windows: a surface still in the pending map would
      // have beaten twice and kept its server row alive past the TTL —
      // the ghost-presence bug this goodbye exists to end.
      act(() => {
        vi.advanceTimersByTime(20_000);
      });
      expect(heartbeatPresence).not.toHaveBeenCalled();
      unmount();
    } finally {
      vi.useRealTimers();
    }
  });
});
