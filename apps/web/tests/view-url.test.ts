import { afterEach, describe, expect, it, vi } from "vitest";
import { isView, pushViewToUrl, viewFromUrl } from "../components/view-url";

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

describe("pushViewToUrl", () => {
  afterEach(() => {
    // jsdom's real history/location were mutated; reset to a clean path.
    window.history.replaceState({}, "", "/");
  });

  it("pushes a history entry with the view param and keeps other params", () => {
    window.history.replaceState({}, "", "/?space=alpha");
    const pushState = vi.spyOn(window.history, "pushState");
    pushViewToUrl("memory");
    expect(pushState).toHaveBeenCalledTimes(1);
    expect(window.location.search).toBe("?space=alpha&view=memory");
    pushState.mockRestore();
  });

  it("no-ops when the URL already holds this view", () => {
    window.history.replaceState({}, "", "/?view=chat");
    const pushState = vi.spyOn(window.history, "pushState");
    pushViewToUrl("chat");
    expect(pushState).not.toHaveBeenCalled();
    expect(window.location.search).toBe("?view=chat");
    pushState.mockRestore();
  });
});

