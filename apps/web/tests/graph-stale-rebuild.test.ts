import { cleanup, render, screen } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { KnowledgeGraph } from "@workspace/api-client";
import { GraphView } from "../components/views/graph";

/**
 * Who acts on a stale projection.
 *
 * The graph is derived from two authoritative inputs — indexed passages and
 * long-term memory — and every write to either marks the projection stale. Only
 * the source paths ever followed that up with a rebuild, so a workspace that
 * learns from conversation and has uploaded no documents sat stale forever: the
 * memories were there, their entities were extracted and on screen in the memory
 * rows, and no code path would ever project them.
 *
 * The repair belongs here rather than on GET /api/graph because the workspace
 * re-reads the graph after every chat turn, and a rebuild is up to sixty
 * extraction calls. This view mounts only when the tab is open, which makes the
 * rebuild cost one-per-visit-that-finds-it-stale instead of one-per-turn.
 */
const EMPTY: KnowledgeGraph = {
  status: "stale",
  version: "",
  built_at: null,
  entities: [],
  edges: [],
};

function view(graph: KnowledgeGraph, rebuild: () => Promise<void>) {
  return createElement(GraphView, {
    graph,
    rebuild,
    openChunk: async () => undefined,
  });
}

afterEach(cleanup);

describe("the knowledge graph page", () => {
  it("rebuilds a stale projection when it is opened", () => {
    const rebuild = vi.fn().mockResolvedValue(undefined);
    render(view(EMPTY, rebuild));
    expect(rebuild).toHaveBeenCalledTimes(1);
  });

  it("asks once, however often the poll re-renders it", () => {
    const rebuild = vi.fn().mockResolvedValue(undefined);
    // A new object each time is what the polling `setGraph` actually produces,
    // so an effect keyed on identity rather than on status would refire — and
    // each refire is another full rebuild.
    const { rerender } = render(view({ ...EMPTY }, rebuild));
    rerender(view({ ...EMPTY }, rebuild));
    rerender(view({ ...EMPTY }, rebuild));
    expect(rebuild).toHaveBeenCalledTimes(1);
  });

  it("leaves a ready projection alone", () => {
    const rebuild = vi.fn().mockResolvedValue(undefined);
    render(view({ ...EMPTY, status: "ready" }, rebuild));
    expect(rebuild).not.toHaveBeenCalled();
  });

  it("does not answer 'no graph yet' while one is on the way", () => {
    render(view({ ...EMPTY, status: "building" }, vi.fn().mockResolvedValue(undefined)));
    expect(screen.queryByText("No graph yet")).toBeNull();
    expect(screen.getByText("Building the graph")).toBeTruthy();
  });

  it("keeps the manual rebuild reachable when the automatic one did not take", () => {
    // Status stays stale when the POST fails. Disabling the button on stale —
    // which reads as "in flight" — would strand the user with no way to retry.
    render(view(EMPTY, vi.fn().mockRejectedValue(new Error("offline"))));
    const button = screen.getByRole("button", { name: /Rebuild/ }) as HTMLButtonElement;
    expect(button.disabled).toBe(false);
  });
});
