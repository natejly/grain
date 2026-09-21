import type { Followup } from "@workspace/api-client";
import { describe, expect, it } from "vitest";
import {
  describeFollowup,
  orderFollowups,
  seededDraft,
} from "../components/views/followup-format";

/**
 * The chips are an enhancement, so the interesting cases are the ones where
 * they could do HARM: clobbering typed work, or ordering non-deterministically
 * (which would make the server's eval gate unmeasurable from the outside).
 */

function chip(overrides: Partial<Followup> = {}): Followup {
  return {
    text: "What does the workspace say about retention?",
    origin: "heading",
    probe_score: 0.4,
    chunk_ids: ["chunk-a"],
    ...overrides,
  };
}

describe("chip ordering", () => {
  it("puts knowledge-graph chips before heading chips", () => {
    const ordered = orderFollowups([
      chip({ text: "heading one", origin: "heading", probe_score: 0.9 }),
      chip({ text: "graph one", origin: "kg", probe_score: 0.1 }),
    ]);
    expect(ordered.map((item) => item.text)).toEqual(["graph one", "heading one"]);
  });

  it("is total, so two renders of one payload cannot differ", () => {
    // Same origin, same score: the text breaks the tie rather than the input
    // order, which is what makes the rendered list a function of the payload.
    const ordered = orderFollowups([
      chip({ text: "beta", probe_score: 0.5 }),
      chip({ text: "alpha", probe_score: 0.5 }),
    ]);
    expect(ordered.map((item) => item.text)).toEqual(["alpha", "beta"]);
  });

  it("does not mutate its input", () => {
    const input = [chip({ text: "b", origin: "heading" }), chip({ text: "a", origin: "kg" })];
    orderFollowups(input);
    expect(input.map((item) => item.text)).toEqual(["b", "a"]);
  });

  it("renders nothing for an empty list rather than an empty shelf", () => {
    // "No suggestion cleared the probe" is a real and common answer for a thin
    // corpus. A labelled-but-empty row would read as a failure, so the caller
    // gates on length — and there is nothing here to order.
    expect(orderFollowups([])).toEqual([]);
  });
});

describe("chip hover text", () => {
  it("names where the chip came from and that it was checked", () => {
    expect(describeFollowup(chip({ origin: "kg", chunk_ids: ["a", "b"] }))).toBe(
      "From the knowledge graph — 2 passages in this workspace can answer it.",
    );
    expect(describeFollowup(chip({ origin: "heading", chunk_ids: ["a"] }))).toBe(
      "From a heading in the answer — 1 passage in this workspace can answer it.",
    );
  });
});

describe("seeding the composer", () => {
  it("seeds an empty composer with the chip's text", () => {
    expect(seededDraft("", "What changed?")).toBe("What changed?");
  });

  it("APPENDS to a draft already in flight rather than clobbering it", () => {
    // The drafts are per-thread and remembered across a thread switch, so the
    // text being replaced could be minutes old and not on screen. Losing typed
    // work to a chip click is the failure that would get this turned off.
    expect(seededDraft("half a question", "What changed?")).toBe(
      "half a question\nWhat changed?",
    );
  });

  it("treats a whitespace-only draft as empty", () => {
    expect(seededDraft("   \n ", "What changed?")).toBe("What changed?");
  });

  it("returns text — it never sends", () => {
    // The whole contract in one assertion: seeding is a pure string function,
    // so there is nothing here that COULD send. A chip that sent would let a
    // stray click spend a turn.
    expect(typeof seededDraft("a", "b")).toBe("string");
  });
});
