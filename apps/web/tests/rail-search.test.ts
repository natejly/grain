import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ConversationSearchHit } from "@workspace/api-client";
import { RailSearch, dedupeHits } from "../components/rail-search";

/**
 * The rail's search input is the palette deep-search effect's twin — same
 * index, same debounce, same stale-reply discipline — so these pin exactly
 * the behaviors that would let the two front doors drift: the 3-character
 * floor, the 200ms debounce, dedupe-and-cap, the Enter-into-results model,
 * and the swallowed error. `search` is a prop, so no vi.mock of the api
 * module is needed; fake timers drive the debounce.
 */

function hit(overrides: Partial<ConversationSearchHit> = {}): ConversationSearchHit {
  return {
    conversation_id: "conv-1",
    title: "Launch retro",
    kind: "quote",
    snippet: "the launch landed smoothly and the sign-ups came in",
    spoken_at: "2026-09-18T10:00:00Z",
    ...overrides,
  };
}

function mount(
  search: (q: string) => Promise<ConversationSearchHit[]>,
  openThread: (id: string) => void = () => undefined,
) {
  return render(createElement(RailSearch, { search, openThread }));
}

function type(value: string) {
  fireEvent.change(screen.getByLabelText("Search chats"), {
    target: { value },
  });
}

async function settle(ms = 250) {
  // Run the debounce timer, then let the promise chain flush.
  await act(async () => {
    vi.advanceTimersByTime(ms);
    await Promise.resolve();
  });
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("dedupeHits", () => {
  it("keeps the first hit per conversation and caps the list", () => {
    const rows = [
      hit({ conversation_id: "a", snippet: "first" }),
      hit({ conversation_id: "a", snippet: "second mention" }),
      ...Array.from({ length: 10 }, (_, i) => hit({ conversation_id: `c${i}` })),
    ];
    const deduped = dedupeHits(rows);
    expect(deduped).toHaveLength(8);
    expect(deduped[0].snippet).toBe("first");
    expect(new Set(deduped.map((row) => row.conversation_id)).size).toBe(8);
    expect(dedupeHits(rows, 3)).toHaveLength(3);
  });
});

describe("RailSearch", () => {
  it("never queries below three characters", async () => {
    const search = vi.fn().mockResolvedValue([]);
    mount(search);
    type("la");
    await settle();
    expect(search).not.toHaveBeenCalled();
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("queries once, trimmed, after the 200ms debounce", async () => {
    const search = vi.fn().mockResolvedValue([hit()]);
    mount(search);
    type("  launch  ");
    // Before the debounce fires, nothing has been asked.
    expect(search).not.toHaveBeenCalled();
    await settle();
    expect(search).toHaveBeenCalledTimes(1);
    expect(search).toHaveBeenCalledWith("launch");
  });

  it("renders title, snippet and rows for the hits", async () => {
    const search = vi.fn().mockResolvedValue([hit()]);
    mount(search);
    type("launch");
    await settle();
    const row = screen.getByRole("option");
    expect(row.textContent).toContain("Launch retro");
    expect(row.textContent).toContain("the launch landed smoothly");
  });

  it("Enter first moves the selection into the results, then opens and clears", async () => {
    const openThread = vi.fn();
    const search = vi.fn().mockResolvedValue([hit()]);
    mount(search, openThread);
    const input = screen.getByLabelText("Search chats");
    type("launch");
    await settle();

    // First Enter: selection moves to row 0, nothing opens yet.
    fireEvent.keyDown(input, { key: "Enter" });
    expect(openThread).not.toHaveBeenCalled();
    expect(screen.getByRole("option").getAttribute("aria-selected")).toBe("true");

    // Second Enter: the highlighted thread opens and the input clears.
    fireEvent.keyDown(input, { key: "Enter" });
    expect(openThread).toHaveBeenCalledWith("conv-1");
    expect((input as HTMLInputElement).value).toBe("");
    expect(screen.queryByRole("option")).toBeNull();
  });

  it("Escape clears the query and the list in one step", async () => {
    const search = vi.fn().mockResolvedValue([hit()]);
    mount(search);
    const input = screen.getByLabelText("Search chats");
    type("launch");
    await settle();
    expect(screen.getByRole("option")).toBeTruthy();
    fireEvent.keyDown(input, { key: "Escape" });
    expect((input as HTMLInputElement).value).toBe("");
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("says when nothing said matches", async () => {
    const search = vi.fn().mockResolvedValue([]);
    mount(search);
    type("nothing-like-this");
    await settle();
    expect(screen.getByText("Nothing said matches.")).toBeTruthy();
  });

  it("settles a rejecting search to an honest empty list — no toast, no spinner limbo", async () => {
    const search = vi.fn().mockRejectedValue(new Error("index offline"));
    mount(search);
    type("launch");
    await settle();
    // No rows and no error surface ("no index, no row"), but the state
    // SETTLES: the empty line renders rather than the panel waiting forever.
    expect(screen.queryByRole("option")).toBeNull();
    expect(screen.getByText("Nothing said matches.")).toBeTruthy();
  });

  it("drops the previous query's rows when the new query's fetch errors", async () => {
    const search = vi
      .fn()
      .mockResolvedValueOnce([hit({ title: "Budget thread" })])
      .mockRejectedValue(new Error("index offline"));
    mount(search);
    type("budget");
    await settle();
    expect(screen.getByText("Budget thread")).toBeTruthy();
    // The query changes and its fetch fails: presenting budget's rows under
    // "vacation" would open a thread that matched something else entirely.
    type("vacation");
    await settle();
    expect(screen.queryByText("Budget thread")).toBeNull();
    expect(screen.getByText("Nothing said matches.")).toBeTruthy();
  });

  it("drops a stale reply that resolves after the query changed", async () => {
    let resolveFirst: (rows: ConversationSearchHit[]) => void = () => undefined;
    const search = vi
      .fn()
      .mockImplementationOnce(
        () =>
          new Promise<ConversationSearchHit[]>((resolve) => {
            resolveFirst = resolve;
          }),
      )
      .mockResolvedValue([hit({ conversation_id: "conv-2", title: "Fresh" })]);
    mount(search);
    type("launch");
    await settle();
    // The query changes while the first reply is still in flight …
    type("retro");
    await settle();
    // … and the stale reply then lands, but must not render.
    await act(async () => {
      resolveFirst([hit({ conversation_id: "stale", title: "Stale hit" })]);
      await Promise.resolve();
    });
    expect(screen.queryByText("Stale hit")).toBeNull();
    expect(screen.getByText("Fresh")).toBeTruthy();
  });
});
