import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, describe, expect, it } from "vitest";
import type { MemoryItem } from "@workspace/api-client";
import { MemoryView } from "../components/views/memory";

/**
 * Whose memory a row is, on the row.
 *
 * The list holds two kinds now — the workspace's, which answers every
 * colleague, and the caller's own, which answers nobody else — and the server
 * returns them mixed, without another member's ever appearing. That difference
 * decides who a fact can reach, so leaving it invisible would make the page a
 * worse answer to the question it exists for: "what does it know about me, and
 * who else is being told."
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
    shared: true,
    created_at: "2026-08-01T00:00:00",
    updated_at: "2026-08-01T00:00:00",
    ...overrides,
  };
}

function view(memories: MemoryItem[]) {
  return createElement(MemoryView, {
    memories,
    forgetMemory: async () => undefined,
  });
}

afterEach(cleanup);

describe("the memory page", () => {
  it("says of each memory whether the workspace holds it or only you do", () => {
    render(
      view([
        memory({ id: "shared-1" }),
        memory({ id: "mine-1", shared: false, content: "I prefer terse answers." }),
      ]),
    );
    expect(screen.getByText("shared")).toBeTruthy();
    expect(screen.getByText("personal")).toBeTruthy();
  });

  it("tints the shared badge and leaves the personal one quiet", () => {
    const { container } = render(
      view([
        memory({ id: "shared-1" }),
        memory({ id: "mine-1", shared: false }),
      ]),
    );
    // Accent means shared, which is the semantic the thread rail already set
    // with `.thread-share.shared`. Asserting the class rather than a colour
    // keeps this a statement about the design system, not about a hex value.
    expect(container.querySelector(".memory-scope.shared")).toBeTruthy();
    expect(container.querySelector(".memory-scope.personal")).toBeTruthy();
    expect(container.querySelectorAll(".memory-scope").length).toBe(2);
  });

  it("explains what the badge means rather than only labelling it", () => {
    render(view([memory({ id: "mine-1", shared: false })]));
    expect(
      screen.getByTitle("Only you are answered from this"),
    ).toBeTruthy();
  });

  it("lets the search box filter to one scope", () => {
    // The page is a list of everything, so the only way to ask "what is shared"
    // is to search for it — the word has to be part of what a row matches on.
    render(
      view([
        memory({ id: "shared-1", content: "Alpha" }),
        memory({ id: "mine-1", shared: false, content: "Beta" }),
      ]),
    );
    fireEvent.change(screen.getByLabelText("Search memory"), {
      target: { value: "personal" },
    });
    expect(screen.queryByText("Alpha")).toBeNull();
    expect(screen.getByText("Beta")).toBeTruthy();
  });
});
