import { afterEach, describe, expect, it, vi } from "vitest";
import {
  clearWorkspaceUrl,
  isView,
  pushWorkspaceUrl,
  threadFromUrl,
  viewFromUrl,
} from "../components/view-url";

describe("isView", () => {
  it("accepts every real view and rejects everything else", () => {
    expect(isView("chat")).toBe(true);
    expect(isView("inbox")).toBe(false);
    expect(isView("dashboards")).toBe(true);
    expect(isView(null)).toBe(false);
    expect(isView(undefined)).toBe(false);
    expect(isView("")).toBe(false);
    expect(isView("Chat")).toBe(false);
  });
});

describe("viewFromUrl", () => {
  it("reads the view param", () => {
    expect(viewFromUrl("?view=inbox")).toBeNull();
    expect(viewFromUrl("?view=chat")).toBe("chat");
    expect(viewFromUrl("?view=dashboards")).toBe("dashboards");
  });

  it("returns null when the param is absent or names a non-view", () => {
    expect(viewFromUrl("")).toBeNull();
    expect(viewFromUrl("?foo=bar")).toBeNull();
    expect(viewFromUrl("?view=garbage")).toBeNull();
  });

  it("leaves other params alone", () => {
    expect(viewFromUrl("?space=alpha&view=memory")).toBe("memory");
  });
});

describe("threadFromUrl", () => {
  it("reads the raw t param", () => {
    // No validation here on purpose: a thread id is only checkable against the
    // loaded conversations, so the consumer holds the fence (like isView's).
    expect(threadFromUrl("?t=conv-1")).toBe("conv-1");
    expect(threadFromUrl("?view=chat&t=abc123")).toBe("abc123");
  });

  it("returns null when the param is absent", () => {
    expect(threadFromUrl("")).toBeNull();
    expect(threadFromUrl("?view=chat")).toBeNull();
  });
});

describe("pushWorkspaceUrl", () => {
  afterEach(() => {
    window.history.replaceState({}, "", "/");
  });

  it("sets both params for a focused chat thread", () => {
    window.history.replaceState({}, "", "/");
    const pushState = vi.spyOn(window.history, "pushState");
    pushWorkspaceUrl("chat", "conv-1");
    expect(pushState).toHaveBeenCalledTimes(1);
    expect(window.location.search).toBe("?view=chat&t=conv-1");
    pushState.mockRestore();
  });

  it("deletes t when the view is not chat", () => {
    // A stale thread param under another view would deep-link to a screen
    // that never reads it.
    window.history.replaceState({}, "", "/?view=chat&t=conv-1");
    const pushState = vi.spyOn(window.history, "pushState");
    pushWorkspaceUrl("memory", "conv-1");
    expect(pushState).toHaveBeenCalledTimes(1);
    expect(window.location.search).toBe("?view=memory");
    pushState.mockRestore();
  });

  it("deletes t on chat with no focused thread", () => {
    window.history.replaceState({}, "", "/?view=chat&t=conv-1");
    pushWorkspaceUrl("chat", null);
    expect(window.location.search).toBe("?view=chat");
  });

  it("no-ops (zero pushState calls) when both params already match", () => {
    // The load-bearing guard: coalesced or repeated writes must never stack
    // history entries, or back/forward hops through ghosts.
    window.history.replaceState({}, "", "/?view=chat&t=conv-1");
    const pushState = vi.spyOn(window.history, "pushState");
    pushWorkspaceUrl("chat", "conv-1");
    expect(pushState).not.toHaveBeenCalled();
    window.history.replaceState({}, "", "/?view=memory");
    pushWorkspaceUrl("memory", null);
    expect(pushState).not.toHaveBeenCalled();
    pushState.mockRestore();
  });

  it("preserves unrelated params", () => {
    window.history.replaceState({}, "", "/?space=alpha");
    pushWorkspaceUrl("chat", "conv-1");
    expect(window.location.search).toBe("?space=alpha&view=chat&t=conv-1");
    pushWorkspaceUrl("memory", null);
    expect(window.location.search).toBe("?space=alpha&view=memory");
  });
});

describe("clearWorkspaceUrl", () => {
  afterEach(() => {
    window.history.replaceState({}, "", "/");
  });

  it("drops both workspace params in place — a correction, not a navigation", () => {
    window.history.replaceState({}, "", "/?view=chat&t=conv-from-workspace-a");
    const pushState = vi.spyOn(window.history, "pushState");
    const replaceState = vi.spyOn(window.history, "replaceState");

    clearWorkspaceUrl();

    // The foreign thread id is gone from the bar before the new workspace
    // adopts the URL — the known-thread fence stops the OPEN, this stops the
    // address bar (and any link copied from it) from lying.
    expect(window.location.search).toBe("");
    expect(replaceState).toHaveBeenCalledTimes(1);
    expect(pushState).not.toHaveBeenCalled();
    pushState.mockRestore();
    replaceState.mockRestore();
  });

  it("leaves unrelated params alone and no-ops on a bare URL", () => {
    window.history.replaceState({}, "", "/?space=alpha&view=memory");
    clearWorkspaceUrl();
    expect(window.location.search).toBe("?space=alpha");

    const replaceState = vi.spyOn(window.history, "replaceState");
    clearWorkspaceUrl();
    expect(replaceState).not.toHaveBeenCalled();
    replaceState.mockRestore();
  });
});

