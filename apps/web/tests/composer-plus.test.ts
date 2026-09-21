import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { createElement, createRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChatView, type ChatViewProps } from "../components/views/chat";

/**
 * The composer's "+" menu is exposure, not capability: every row reuses a
 * handler that already exists, and a row whose handler was not wired must not
 * render at all — a door drawn on a wall is worse than no door. These pin the
 * conditional rows and that the navigation rows call `openView` with the view
 * they name.
 */

const BASE: ChatViewProps = {
  messages: [],
  sources: [],
  agentCalls: [],
  apps: [],
  draft: "",
  setDraft: () => undefined,
  activeRun: null,
  runStatus: "",
  budgetPark: null,
  submitPrompt: async () => undefined,
  cancelActiveRun: async () => undefined,
  regenerate: async () => undefined,
  decideAgentCall: async () => undefined,
  openCitation: async () => undefined,
  endRef: createRef<HTMLDivElement>(),
};

function view(props: Partial<ChatViewProps> = {}) {
  return render(createElement(ChatView, { ...BASE, ...props }));
}

/** Open the "+" menu and return a scope over it — the Attach and Skills rows
 *  share their accessible names with the chips they duplicate by design. */
function openMenu() {
  fireEvent.click(screen.getByRole("button", { name: "Open tools" }));
  return within(screen.getByRole("group", { name: "Tools" }));
}

afterEach(cleanup);

describe("the composer '+' chip", () => {
  it("opens and closes the tools menu, and says which it did", () => {
    view();
    const chip = screen.getByRole("button", { name: "Open tools" });
    expect(chip.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("group", { name: "Tools" })).toBeNull();
    fireEvent.click(chip);
    expect(chip.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("group", { name: "Tools" })).toBeTruthy();
    fireEvent.click(chip);
    expect(screen.queryByRole("group", { name: "Tools" })).toBeNull();
  });
});

describe("the navigation rows", () => {
  it("render only when openView is wired", () => {
    view();
    const menu = openMenu();
    for (const label of ["Sources", "Datasets", "Gallery"]) {
      expect(menu.queryByRole("button", { name: label })).toBeNull();
    }
  });

  it("call openView with the view they name, then close the menu", () => {
    for (const [label, target] of [
      ["Sources", "sources"],
      ["Datasets", "datasets"],
      ["Gallery", "gallery"],
    ] as const) {
      const openView = vi.fn();
      view({ openView });
      const menu = openMenu();
      fireEvent.click(menu.getByRole("button", { name: label }));
      expect(openView).toHaveBeenCalledWith(target);
      // Closes on pick, like the attach popover it is modeled on.
      expect(screen.queryByRole("group", { name: "Tools" })).toBeNull();
      cleanup();
    }
  });
});

describe("the handler rows", () => {
  it("offers 'Attach a file' only when the attach prop is present", () => {
    view({ openView: () => undefined });
    const bare = openMenu();
    expect(bare.queryByRole("button", { name: "Attach a file" })).toBeNull();
    cleanup();
    view({ attach: { upload: async () => null, uploading: false } });
    const menu = openMenu();
    fireEvent.click(menu.getByRole("button", { name: "Attach a file" }));
    // The row reuses the Attach chip's handler: the popover itself opens.
    expect(screen.getByRole("group", { name: "Attach a file" })).toBeTruthy();
  });

  it("offers 'Use a skill' with the Skills chip's exact handler", () => {
    const setDraft = vi.fn();
    view({
      setDraft,
      skills: {
        attached: null,
        argValues: {},
        attach: () => undefined,
        detach: () => undefined,
        setArg: () => undefined,
      },
    });
    const menu = openMenu();
    fireEvent.click(menu.getByRole("button", { name: "Use a skill" }));
    expect(setDraft).toHaveBeenCalledWith("/");
  });

  it("offers 'Do this on a schedule…' only when scheduleDraft is wired, passing the draft verbatim", () => {
    view({ openView: () => undefined });
    const bare = openMenu();
    expect(
      bare.queryByRole("button", { name: "Do this on a schedule…" }),
    ).toBeNull();
    cleanup();

    const scheduleDraft = vi.fn();
    view({ draft: "Summarise yesterday's PRs", scheduleDraft });
    const menu = openMenu();
    fireEvent.click(menu.getByRole("button", { name: "Do this on a schedule…" }));
    expect(scheduleDraft).toHaveBeenCalledWith("Summarise yesterday's PRs");
    // Closes on pick, like every other row.
    expect(screen.queryByRole("group", { name: "Tools" })).toBeNull();
    cleanup();

    // Still rendered with an empty draft — a row that appears only when
    // text exists is a moving menu — but DISABLED, with the title saying
    // why: the Crons composer only opens over a seed, so a click here
    // would navigate to a page with the composer closed and nothing
    // saying what happened.
    const blank = vi.fn();
    view({ draft: "", scheduleDraft: blank });
    const row = openMenu().getByRole("button", {
      name: "Do this on a schedule…",
    }) as HTMLButtonElement;
    expect(row.disabled).toBe(true);
    expect(row.title).toBe("Type the prompt to schedule first");
    fireEvent.click(row);
    expect(blank).not.toHaveBeenCalled();
    // A whitespace-only draft is an empty draft.
    cleanup();
    view({ draft: "   ", scheduleDraft: blank });
    expect(
      (
        openMenu().getByRole("button", {
          name: "Do this on a schedule…",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
  });

  it("hides 'Use a skill' while a run streams — same gate as the chip", () => {
    view({
      activeRun: "run-1",
      skills: {
        attached: null,
        argValues: {},
        attach: () => undefined,
        detach: () => undefined,
        setArg: () => undefined,
      },
    });
    const menu = openMenu();
    expect(menu.queryByRole("button", { name: "Use a skill" })).toBeNull();
  });
});
