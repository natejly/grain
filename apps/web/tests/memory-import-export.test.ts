// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { MemoryItem } from "@workspace/api-client";
import {
  MemoryView,
  buildMemoryExport,
  importSummaryLine,
  parseMemoryImport,
} from "../components/views/memory";

/**
 * Memory export/import. The export is client-side from the list the view
 * already holds, in a documented v1 shape; the import door accepts that
 * shape back, a bare JSON array, or plain text one memory per line — and
 * renders the server's accounting as a dismissible line.
 */

afterEach(cleanup);

function item(overrides: Partial<MemoryItem> = {}): MemoryItem {
  return {
    id: "mem-1",
    conversation_id: null,
    kind: "fact",
    content: "The staging cluster lives in eu-west-1.",
    entity_names: ["staging"],
    message_ids: [],
    importance: 1,
    shared: false,
    space_id: "",
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-02T00:00:00Z",
    ...overrides,
  };
}

describe("buildMemoryExport", () => {
  it("pins the documented v1 shape", () => {
    const exported = buildMemoryExport([
      item(),
      item({ id: "mem-2", shared: true, kind: "preference", space_id: "sp-1" }),
    ]);
    expect(exported.format).toBe("grain-memory-export");
    expect(exported.version).toBe(1);
    expect(typeof exported.exported_at).toBe("string");
    expect(exported.items).toHaveLength(2);
    expect(exported.items[0]).toEqual({
      id: "mem-1",
      content: "The staging cluster lives in eu-west-1.",
      kind: "fact",
      shared: false,
      space_id: "",
      entity_names: ["staging"],
      created_at: "2026-09-01T00:00:00Z",
      updated_at: "2026-09-02T00:00:00Z",
    });
    expect(exported.items[1].shared).toBe(true);
  });
});

describe("parseMemoryImport", () => {
  it("reads a v1 export back into the items payload", () => {
    const raw = JSON.stringify(buildMemoryExport([item()]));
    expect(parseMemoryImport(raw)).toEqual({
      items: [
        {
          content: "The staging cluster lives in eu-west-1.",
          kind: "fact",
          entities: ["staging"],
        },
      ],
    });
  });

  it("reads a bare JSON array of rows or strings", () => {
    const raw = JSON.stringify([
      { content: "A structured row.", kind: "preference" },
      "A bare sentence.",
      { not_content: "dropped" },
    ]);
    expect(parseMemoryImport(raw)).toEqual({
      items: [
        { content: "A structured row.", kind: "preference", entities: [] },
        { content: "A bare sentence." },
      ],
    });
  });

  it("falls back to the text door for anything that is not JSON", () => {
    expect(parseMemoryImport("First fact.\nSecond fact.\n")).toEqual({
      text: "First fact.\nSecond fact.\n",
    });
  });
});

describe("the Memory view's controls", () => {
  const forget = async () => undefined;

  it("keeps both controls off a bare mount", () => {
    render(
      createElement(MemoryView, { memories: [], forgetMemory: forget }),
    );
    expect(screen.queryByRole("button", { name: /Download memory/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Import/ })).toBeNull();
  });

  it("imports a file and renders the accounting as a dismissible line", async () => {
    const importMemories = vi
      .fn()
      .mockResolvedValue({ added: 2, reinforced: 1, skipped: 0 });
    render(
      createElement(MemoryView, {
        memories: [item()],
        forgetMemory: forget,
        importMemories,
      }),
    );
    expect(screen.getByRole("button", { name: /Download memory/ })).toBeTruthy();
    const input = screen.getByLabelText("Memory file to import");
    const file = new File(["First fact.\nSecond fact.\nThird fact.\n"], "memories.txt", {
      type: "text/plain",
    });
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() =>
      expect(screen.getByText("2 added, 1 reinforced, 0 skipped")).toBeTruthy(),
    );
    expect(importMemories).toHaveBeenCalledWith({
      text: "First fact.\nSecond fact.\nThird fact.\n",
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Dismiss import summary" }),
    );
    expect(screen.queryByText("2 added, 1 reinforced, 0 skipped")).toBeNull();
  });

  it("phrases the summary line the documented way", () => {
    expect(importSummaryLine({ added: 2, reinforced: 1, skipped: 0 })).toBe(
      "2 added, 1 reinforced, 0 skipped",
    );
  });
});
