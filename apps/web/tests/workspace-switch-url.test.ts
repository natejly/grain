import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

/**
 * Switching workspaces must scrub `?t=`/`?view=` from the address bar.
 *
 * The shell remounts on the new workspace id and re-parks whatever the URL
 * says; the known-thread fence already refuses to OPEN a foreign thread id,
 * but nothing used to rewrite the URL — so after A→B the bar still read
 * `?view=chat&t=<A's thread>` over B's screen, and copying "this thread" for
 * a teammate handed them a link that silently degrades to B's default.
 */

const setWorkspaceId = vi.fn();

vi.mock("../components/api", () => ({
  api: {
    listWorkspaces: () =>
      Promise.resolve([
        { id: "w-1", name: "One", role: "owner", is_current: true },
        { id: "w-2", name: "Two", role: "member", is_current: false },
      ]),
    setWorkspaceId: (...a: unknown[]) => setWorkspaceId(...a),
  },
}));

import {
  WorkspaceSelection,
  useWorkspaceSelection,
} from "../components/workspace-selection";

/** A child that exposes `select` the way the switcher's rows do. */
function Probe() {
  const { currentId, select } = useWorkspaceSelection();
  return createElement(
    "button",
    { onClick: () => select("w-2") },
    `in:${currentId}`,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.history.replaceState({}, "", "/");
  window.localStorage.clear();
});

describe("select() and the address bar", () => {
  it("clears ?t= and ?view= in place on a workspace switch", async () => {
    window.history.replaceState({}, "", "/?view=chat&t=thread-from-w1&space=alpha");
    const pushState = vi.spyOn(window.history, "pushState");
    render(createElement(WorkspaceSelection, null, createElement(Probe)));
    await screen.findByText("in:w-1");

    fireEvent.click(screen.getByRole("button"));

    await waitFor(() => expect(screen.getByText("in:w-2")).toBeTruthy());
    // Both workspace params are gone — the foreign thread id cannot ride
    // into workspace B's URL adoption — while unrelated params survive.
    expect(window.location.search).toBe("?space=alpha");
    // In place: leaving a workspace is not a navigation inside one.
    expect(pushState).not.toHaveBeenCalled();
    expect(setWorkspaceId).toHaveBeenCalledWith("w-2");
    pushState.mockRestore();
  });

  it("touches nothing when the same workspace is re-selected", async () => {
    window.history.replaceState({}, "", "/?view=chat&t=thread-from-w1");
    function SameProbe() {
      const { currentId, select } = useWorkspaceSelection();
      return createElement(
        "button",
        { onClick: () => select("w-1") },
        `in:${currentId}`,
      );
    }
    render(createElement(WorkspaceSelection, null, createElement(SameProbe)));
    await screen.findByText("in:w-1");

    fireEvent.click(screen.getByRole("button"));

    // No switch, no scrub: the params still belong to this workspace.
    expect(window.location.search).toBe("?view=chat&t=thread-from-w1");
  });
});
