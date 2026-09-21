// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { createElement, createRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { WorkspaceSettingsMenu } from "../components/settings-menu";
import { ChatView, type ChatViewProps } from "../components/views/chat";

/**
 * The response style, on its two doors: the settings menu authors it (the
 * safe-mode null-until-bootstrap pattern exactly), and the composer picker
 * selects it, labelled "· you" so a persistent member preference is never
 * read as per-thread state.
 */

afterEach(cleanup);

function menu(
  stylePreset: string | null,
  customStyleText = "",
  onStyleChange: (preset: string, customText: string) => void = () => undefined,
) {
  return render(
    createElement(WorkspaceSettingsMenu, {
      activeGroup: "chat" as never,
      open: () => undefined,
      digest: null,
      onDigestChange: () => undefined,
      safeMode: false,
      onSafeModeChange: () => undefined,
      stylePreset,
      customStyleText,
      onStyleChange,
    }),
  );
}

const openMenu = () =>
  fireEvent.click(screen.getByRole("button", { name: "Workspace settings" }));

describe("the settings-menu style section", () => {
  it("never renders before the preference has been read", () => {
    menu(null);
    openMenu();
    expect(screen.queryByRole("combobox", { name: "Response style" })).toBeNull();
    expect(screen.queryByText("Response style")).toBeNull();
  });

  it("stays hidden on a bare mount, so older call sites stand unchanged", () => {
    render(
      createElement(WorkspaceSettingsMenu, {
        activeGroup: "chat" as never,
        open: () => undefined,
        digest: null,
        onDigestChange: () => undefined,
        safeMode: false,
        onSafeModeChange: () => undefined,
      }),
    );
    openMenu();
    expect(screen.queryByRole("combobox", { name: "Response style" })).toBeNull();
  });

  it("renders the active preset", () => {
    menu("concise");
    openMenu();
    const select = screen.getByRole("combobox", {
      name: "Response style",
    }) as HTMLSelectElement;
    expect(select.value).toBe("concise");
  });

  it("reports a fixed preset on pick, carrying the custom text along", () => {
    const seen: [string, string][] = [];
    menu("normal", "kept prose", (preset, text) => seen.push([preset, text]));
    openMenu();
    fireEvent.change(screen.getByRole("combobox", { name: "Response style" }), {
      target: { value: "formal" },
    });
    expect(seen).toEqual([["formal", "kept prose"]]);
  });

  it("reveals the textarea for custom and saves only with text", () => {
    const seen: [string, string][] = [];
    menu("normal", "", (preset, text) => seen.push([preset, text]));
    openMenu();
    fireEvent.change(screen.getByRole("combobox", { name: "Response style" }), {
      target: { value: "custom" },
    });
    // Picking custom saves nothing yet — an empty custom block is
    // unrepresentable server-side.
    expect(seen).toEqual([]);
    const textarea = screen.getByRole("textbox", {
      name: "Custom style instructions",
    });
    const save = screen.getByRole("button", { name: "Save style" });
    expect((save as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(textarea, { target: { value: "Answer in haiku." } });
    fireEvent.click(save);
    expect(seen).toEqual([["custom", "Answer in haiku."]]);
  });

  it("names the boundary: Normal means no style instruction at all", () => {
    menu("normal");
    openMenu();
    expect(
      screen.getByText(/Normal means no style\s+instruction at all/),
    ).toBeTruthy();
    expect(
      screen.getByText(/Applies to your future turns in every thread/),
    ).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// The composer picker

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

describe("the composer style picker", () => {
  it("renders only when wired — side-panel mounts show nothing", () => {
    render(createElement(ChatView, BASE));
    expect(
      screen.queryByRole("combobox", { name: "Response style · you" }),
    ).toBeNull();
  });

  it("shows the active style, labelled with the member scope", () => {
    render(
      createElement(ChatView, {
        ...BASE,
        responseStyle: {
          preset: "explanatory",
          customText: "",
          onChange: () => undefined,
        },
      }),
    );
    const select = screen.getByRole("combobox", {
      name: "Response style · you",
    }) as HTMLSelectElement;
    expect(select.value).toBe("explanatory");
    // Custom is offered only once custom text exists — the composer picker
    // selects, it does not author.
    expect(
      Array.from(select.options).map((option) => option.value),
    ).toEqual(["normal", "concise", "explanatory", "formal"]);
  });

  it("offers Custom once custom text exists, and reports through the shared handler", () => {
    const onChange = vi.fn();
    render(
      createElement(ChatView, {
        ...BASE,
        responseStyle: {
          preset: "normal",
          customText: "Answer in haiku.",
          onChange,
        },
      }),
    );
    const select = screen.getByRole("combobox", {
      name: "Response style · you",
    }) as HTMLSelectElement;
    expect(Array.from(select.options).map((option) => option.value)).toContain(
      "custom",
    );
    fireEvent.change(select, { target: { value: "custom" } });
    expect(onChange).toHaveBeenCalledWith("custom", "Answer in haiku.");
  });
});
