import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const listCrons = vi.fn();
const workflowSchedulingEnabled = vi.fn();

vi.mock("../components/api", () => ({
  api: {
    listCrons: (...a: unknown[]) => listCrons(...a),
    workflowSchedulingEnabled: (...a: unknown[]) => workflowSchedulingEnabled(...a),
  },
}));

import { CronsView } from "../components/views/crons";

/**
 * Schedule-from-chat's seed consumption, mirroring WorkflowsView's
 * composeRequested/onComposeHandled contract: the shell raises the flag with
 * the chat draft, the panel copies it local FIRST and lowers the flag, so a
 * later re-render with the flag cleared keeps the composer open and the
 * prompt intact — and navigating back later does not reopen a composer the
 * user dismissed.
 */

function mount(props: Record<string, unknown> = {}) {
  return render(
    createElement(CronsView, {
      setError: () => undefined,
      ...props,
    }),
  );
}

beforeEach(() => {
  listCrons.mockResolvedValue([]);
  workflowSchedulingEnabled.mockResolvedValue(false);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("CronsView composeSeed", () => {
  it("opens the New-automation form with the prompt prefilled and kind on task", async () => {
    const onComposeSeedHandled = vi.fn();
    mount({ composeSeed: "Summarise yesterday", onComposeSeedHandled });
    await screen.findByRole("heading", { name: "New automation" });
    const prompt = screen.getByLabelText("Prompt") as HTMLTextAreaElement;
    expect(prompt.value).toBe("Summarise yesterday");
    const kind = screen.getByLabelText("Kind") as HTMLSelectElement;
    expect(kind.value).toBe("task");
    expect(onComposeSeedHandled).toHaveBeenCalledTimes(1);
  });

  it("keeps the form open and the prompt intact once the parent clears the seed", async () => {
    const onComposeSeedHandled = vi.fn();
    const view = mount({ composeSeed: "Summarise yesterday", onComposeSeedHandled });
    await screen.findByRole("heading", { name: "New automation" });

    // The shell lowers the flag — the local copy carries on.
    view.rerender(
      createElement(CronsView, {
        setError: () => undefined,
        composeSeed: "",
        onComposeSeedHandled,
      }),
    );
    expect(screen.getByRole("heading", { name: "New automation" })).toBeTruthy();
    expect((screen.getByLabelText("Prompt") as HTMLTextAreaElement).value).toBe(
      "Summarise yesterday",
    );
    expect(onComposeSeedHandled).toHaveBeenCalledTimes(1);
  });

  it("shows the ordinary empty state with no seed", async () => {
    mount();
    expect(
      await screen.findByText("Pick an automation, or schedule a new one."),
    ).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "New automation" })).toBeNull();
  });

  it("keeps the Plus button opening a blank form", async () => {
    mount({ composeSeed: "Summarise yesterday", onComposeSeedHandled: () => undefined });
    await screen.findByRole("heading", { name: "New automation" });
    fireEvent.click(screen.getByRole("button", { name: "New automation" }));
    expect((screen.getByLabelText("Prompt") as HTMLTextAreaElement).value).toBe("");
  });
});
