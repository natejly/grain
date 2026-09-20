import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { MemoryItem } from "@workspace/api-client";
import { MemoryView, type MemoryViewProps } from "../components/views/memory";

/**
 * The Memory page's manual add and inline edit.
 *
 * Three contracts are pinned. The bare-mount one: both handlers are optional,
 * so a MemoryView mounted without them — every older test, the panels that
 * only read — renders exactly the page it always did, no Add button, no
 * pencils. The scope one: the add form chooses between "mine" and
 * "everyone's" and NOTHING else — no space picker, because "" is a sentinel
 * and a control that could clear a scope would be a silent promotion. And the
 * summary one: the rolling summary rewrites itself, so its row never grows a
 * pencil the server would answer with a 422.
 */

function memory(overrides: Partial<MemoryItem> = {}): MemoryItem {
  return {
    id: "m1",
    conversation_id: null,
    space_id: "",
    kind: "fact",
    content: "The API deploys on Railway.",
    entity_names: [],
    message_ids: [],
    importance: 1,
    shared: false,
    created_at: "2026-08-01T00:00:00",
    updated_at: "2026-08-01T00:00:00",
    ...overrides,
  };
}

function view(memories: MemoryItem[], extra: Partial<MemoryViewProps> = {}) {
  return render(
    createElement(MemoryView, {
      memories,
      forgetMemory: async () => undefined,
      ...extra,
    }),
  );
}

afterEach(cleanup);

describe("the bare mount", () => {
  it("offers neither add nor edit when the handlers are absent", () => {
    view([memory()]);
    expect(screen.queryByRole("button", { name: /Add memory/ })).toBeNull();
    expect(screen.queryByLabelText("Edit this memory")).toBeNull();
    // The page it always was: the row and its forget button still stand.
    expect(screen.getByText("The API deploys on Railway.")).toBeTruthy();
    expect(screen.getByLabelText("Forget this memory")).toBeTruthy();
  });
});

describe("adding a memory by hand", () => {
  it("opens the form, submits content/kind/shared, and closes", async () => {
    const seen: Array<{ content: string; kind: string; shared: boolean }> = [];
    view([memory()], {
      addMemory: async (input) => {
        seen.push(input);
      },
    });
    fireEvent.click(screen.getByRole("button", { name: /Add memory/ }));
    fireEvent.change(screen.getByLabelText("New memory"), {
      target: { value: "Deploys go out on Fridays." },
    });
    fireEvent.change(screen.getByLabelText("Kind of memory"), {
      target: { value: "preference" },
    });
    fireEvent.click(screen.getByRole("checkbox", { name: /Share with the workspace/ }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(seen).toHaveLength(1));
    expect(seen[0]).toEqual({
      content: "Deploys go out on Fridays.",
      kind: "preference",
      shared: true,
    });
    // The form clears and closes once the save lands.
    await waitFor(() => expect(screen.queryByLabelText("New memory")).toBeNull());
  });

  it("offers no space control at all — scope is mine or everyone's, never a shelf", () => {
    view([memory()], { addMemory: async () => undefined });
    fireEvent.click(screen.getByRole("button", { name: /Add memory/ }));
    // The one select is the kind; a space picker would let a click promote a
    // sentence onto another shelf, which the '' sentinel makes irreversible
    // from here.
    expect(screen.getAllByRole("combobox")).toHaveLength(1);
    expect(screen.getByLabelText("Kind of memory")).toBeTruthy();
  });

  it("says on the shared checkbox exactly what the rows' badge says", () => {
    view([memory()], { addMemory: async () => undefined });
    fireEvent.click(screen.getByRole("button", { name: /Add memory/ }));
    expect(
      screen.getByTitle("Every member of this workspace is answered from this"),
    ).toBeTruthy();
  });

  it("refuses an empty save", () => {
    view([memory()], { addMemory: async () => undefined });
    fireEvent.click(screen.getByRole("button", { name: /Add memory/ }));
    const save = screen.getByRole("button", { name: "Save" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
  });

  it("still offers the door when nothing is remembered yet", () => {
    view([], { addMemory: async () => undefined });
    expect(screen.getByRole("button", { name: /Add memory/ })).toBeTruthy();
  });
});

describe("editing a memory in place", () => {
  it("shows the pencil on fact rows and never on the rolling summary", () => {
    view(
      [
        memory({ id: "fact-1" }),
        memory({ id: "sum-1", kind: "summary", content: "Lately: deploys." }),
      ],
      { editMemory: async () => undefined },
    );
    // One pencil — the fact's. The summary rewrites itself; the server
    // answers a PATCH of it with a 422, so the door is not drawn.
    expect(screen.getAllByLabelText("Edit this memory")).toHaveLength(1);
  });

  it("prefills the row's sentence and hands the handler the new one", async () => {
    const seen: Array<[string, string]> = [];
    const item = memory();
    view([item], {
      editMemory: async (edited, content) => {
        seen.push([edited.id, content]);
      },
    });
    fireEvent.click(screen.getByLabelText("Edit this memory"));
    const area = screen.getByLabelText("Edit this memory", {
      selector: "textarea",
    }) as HTMLTextAreaElement;
    expect(area.value).toBe("The API deploys on Railway.");
    fireEvent.change(area, { target: { value: "The API deploys on Render." } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(seen).toEqual([["m1", "The API deploys on Render."]]));
    // The editor closes; content only — scope and space were never resent.
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Save" })).toBeNull(),
    );
  });

  it("cancel puts the sentence back untouched", () => {
    view([memory()], { editMemory: async () => undefined });
    fireEvent.click(screen.getByLabelText("Edit this memory"));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByText("The API deploys on Railway.")).toBeTruthy();
  });
});

describe("a refused write keeps the draft", () => {
  // The handlers resolve `false` when the server said no — the dedup 409
  // ("Another memory already says this"), a network blip. Closing the form
  // over that used to discard up to 900 typed characters while the error
  // strip said the save never happened.
  it("keeps the add form open, sentence intact, when addMemory reports false", async () => {
    const addMemory = vi.fn().mockResolvedValue(false);
    view([memory()], { addMemory });
    fireEvent.click(screen.getByRole("button", { name: /Add memory/ }));
    fireEvent.change(screen.getByLabelText("New memory"), {
      target: { value: "A sentence the server will refuse." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(addMemory).toHaveBeenCalledTimes(1));
    const area = screen.getByLabelText("New memory") as HTMLTextAreaElement;
    expect(area.value).toBe("A sentence the server will refuse.");
  });

  it("keeps the inline editor open with the rewrite when editMemory reports false", async () => {
    const editMemory = vi.fn().mockResolvedValue(false);
    view([memory()], { editMemory });
    fireEvent.click(screen.getByLabelText("Edit this memory"));
    const area = screen.getByLabelText("Edit this memory", {
      selector: "textarea",
    }) as HTMLTextAreaElement;
    fireEvent.change(area, { target: { value: "A duplicate of another row." } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(editMemory).toHaveBeenCalledTimes(1));
    // The editor did not snap back to the old sentence: the draft is still
    // there to adjust, exactly as typed.
    expect(
      (screen.getByLabelText("Edit this memory", { selector: "textarea" }) as HTMLTextAreaElement)
        .value,
    ).toBe("A duplicate of another row.");
  });

  it("still closes on the accepted write", async () => {
    const editMemory = vi.fn().mockResolvedValue(true);
    view([memory()], { editMemory });
    fireEvent.click(screen.getByLabelText("Edit this memory"));
    fireEvent.change(
      screen.getByLabelText("Edit this memory", { selector: "textarea" }),
      { target: { value: "The API deploys on Render." } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Save" })).toBeNull(),
    );
  });
});
