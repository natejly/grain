import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { CommandPalette } from "../components/command-palette";
import { buildPaletteRows, matchPalette } from "../components/views/palette";

/**
 * Quick-compose in ⌘K, both halves. The pure half pins the row model — the
 * static "New message…" row exists only where a compose handler is wired,
 * and "> text" is a mode where Enter can only ever mean send. The component
 * half pins the wiring: the '>' fast path, the NamingTask second step the
 * empty row reuses, and the guard that keeps a compose draft out of the
 * transcript index.
 */

afterEach(cleanup);

// jsdom draws no boxes, so the roving-focus effect's scrollIntoView is absent;
// stub it rather than let a layout nicety fail behavioral tests.
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn();
});

describe("buildPaletteRows compose row", () => {
  it("appears only when compose is wired, and the two-arg call stays back-compatible", () => {
    const withCompose = buildPaletteRows([], undefined, true);
    const composeRows = withCompose.filter((row) => row.kind === "compose");
    expect(composeRows).toHaveLength(1);
    expect(composeRows[0]).toMatchObject({
      text: "",
      label: "New message…",
      hint: "Chat · or type > message",
    });
    expect(
      buildPaletteRows([], undefined, false).some((row) => row.kind === "compose"),
    ).toBe(false);
    expect(buildPaletteRows([]).some((row) => row.kind === "compose")).toBe(false);
  });

  it("rides the empty-query listing like any non-thread row", () => {
    const rows = buildPaletteRows([], undefined, true);
    const empty = matchPalette(rows, "");
    expect(empty.some((row) => row.kind === "compose")).toBe(true);
  });

  it("ranks the static row by its label in the tiers", () => {
    const rows = buildPaletteRows([], undefined, true);
    const matches = matchPalette(rows, "new mess");
    expect(matches[0]).toMatchObject({ kind: "compose", label: "New message…" });
  });
});

describe("matchPalette '>' prefix mode", () => {
  const rows = buildPaletteRows([], undefined, true);

  it("returns exactly one compose row carrying the text", () => {
    const matches = matchPalette(rows, "> ship the report");
    expect(matches).toEqual([
      {
        kind: "compose",
        text: "ship the report",
        label: "Send: “ship the report”",
        hint: "Starts a new thread",
      },
    ]);
  });

  it("answers a bare '>' with the prompt-hint row and empty text", () => {
    const matches = matchPalette(rows, ">");
    expect(matches).toEqual([
      {
        kind: "compose",
        text: "",
        label: "New message…",
        hint: "Type your message after >",
      },
    ]);
  });

  it("returns nothing when the rows carry no compose row", () => {
    const bare = buildPaletteRows([], undefined, false);
    expect(matchPalette(bare, "> hello")).toEqual([]);
  });
});

function mount(props: Record<string, unknown> = {}) {
  return render(
    createElement(CommandPalette, {
      open: true,
      close: () => undefined,
      conversations: [],
      openView: () => undefined,
      openThread: () => undefined,
      create: async () => undefined,
      ...props,
    }),
  );
}

function paletteInput() {
  return screen.getByRole("dialog", { name: "Command palette" }).querySelector("input")!;
}

describe("CommandPalette compose wiring", () => {
  it("'> hello' then Enter calls compose once with the text and closes", async () => {
    const compose = vi.fn().mockResolvedValue(undefined);
    const close = vi.fn();
    mount({ compose, close });
    const input = paletteInput();
    fireEvent.change(input, { target: { value: "> hello" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await Promise.resolve();
    expect(compose).toHaveBeenCalledTimes(1);
    expect(compose).toHaveBeenCalledWith("hello");
    expect(close).toHaveBeenCalled();
  });

  it("the empty 'New message…' row enters the naming step; Enter sends, Escape backs out", async () => {
    const compose = vi.fn().mockResolvedValue(undefined);
    const close = vi.fn();
    mount({ compose, close });
    fireEvent.click(screen.getByRole("option", { name: /New message…/ }));

    // The NamingTask second step: the input becomes the message field.
    const naming = screen.getByPlaceholderText("Message");
    expect(naming).toBeTruthy();

    // Escape backs out to the list without closing the palette.
    fireEvent.keyDown(naming, { key: "Escape" });
    expect(close).not.toHaveBeenCalled();
    expect(screen.queryByPlaceholderText("Message")).toBeNull();
    expect(screen.getByRole("listbox", { name: "Results" })).toBeTruthy();

    // Back in: typed text plus Enter sends and closes.
    fireEvent.click(screen.getByRole("option", { name: /New message…/ }));
    const again = screen.getByPlaceholderText("Message");
    fireEvent.change(again, { target: { value: "morning digest please" } });
    fireEvent.keyDown(again, { key: "Enter" });
    await Promise.resolve();
    expect(compose).toHaveBeenCalledWith("morning digest please");
    expect(close).toHaveBeenCalled();
  });

  it("renders no compose rows without a compose prop", () => {
    mount();
    expect(screen.queryByRole("option", { name: /New message…/ })).toBeNull();
    const input = paletteInput();
    fireEvent.change(input, { target: { value: "> hello" } });
    // The '>' mode collapses to no matches — never a send it cannot honor.
    expect(screen.getByText("Nothing matches.")).toBeTruthy();
  });

  it("a '>' query never reaches the transcript index", () => {
    vi.useFakeTimers();
    try {
      const searchTranscripts = vi.fn().mockResolvedValue([]);
      const compose = vi.fn().mockResolvedValue(undefined);
      mount({ compose, searchTranscripts });
      const input = paletteInput();
      fireEvent.change(input, { target: { value: "> summarise the launch" } });
      vi.advanceTimersByTime(500);
      expect(searchTranscripts).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });
});
