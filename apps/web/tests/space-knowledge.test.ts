import type { Source } from "@workspace/api-client";
import { describe, expect, it } from "vitest";
import {
  SPACE_KNOWLEDGE_SOFT_CEILING_BYTES,
  knowledgeUsage,
} from "../components/views/space-knowledge";
import { sourcesInSpace } from "../components/views/space-threads";

function source(overrides: Partial<Source> = {}): Source {
  return {
    id: "src-1",
    filename: "notes.pdf",
    media_type: "application/pdf",
    byte_size: 1000,
    status: "ready",
    error: "",
    chunk_count: 3,
    space_id: "",
    conversation_id: "",
    created_at: "2026-09-01T00:00:00Z",
    ...overrides,
  };
}

describe("knowledgeUsage", () => {
  it('returns zeros for spaceId "" — the sentinel must never over-match the library', () => {
    const sources = [
      source({ id: "a", space_id: "" }),
      source({ id: "b", space_id: "space-1" }),
    ];
    expect(knowledgeUsage(sources, "")).toEqual({ files: 0, bytes: 0, percent: 0 });
  });

  it("sums byte_size only for the matching space", () => {
    const sources = [
      source({ id: "a", space_id: "space-1", byte_size: 100 }),
      source({ id: "b", space_id: "space-1", byte_size: 250 }),
      source({ id: "c", space_id: "space-2", byte_size: 5000 }),
      source({ id: "d", space_id: "", byte_size: 7000 }),
    ];
    const usage = knowledgeUsage(sources, "space-1");
    expect(usage.files).toBe(2);
    expect(usage.bytes).toBe(350);
  });

  it("caps percent at 100 above the (purely cosmetic) ceiling", () => {
    const sources = [
      source({
        id: "a",
        space_id: "space-1",
        byte_size: SPACE_KNOWLEDGE_SOFT_CEILING_BYTES * 2,
      }),
    ];
    expect(knowledgeUsage(sources, "space-1").percent).toBe(100);
  });

  it("rounds an in-range percent from the ceiling", () => {
    const sources = [
      source({
        id: "a",
        space_id: "space-1",
        byte_size: SPACE_KNOWLEDGE_SOFT_CEILING_BYTES / 2,
      }),
    ];
    expect(knowledgeUsage(sources, "space-1").percent).toBe(50);
  });

  it("agrees row-for-row with sourcesInSpace on the same input", () => {
    // The meter's whole design claim: it sums exactly the rows the list
    // beside it shows, so the two can never disagree.
    const sources = [
      source({ id: "a", space_id: "space-1", byte_size: 10 }),
      source({ id: "b", space_id: "space-2", byte_size: 20 }),
      source({ id: "c", space_id: "space-1", byte_size: 30 }),
      source({ id: "d", space_id: "", byte_size: 40 }),
    ];
    for (const spaceId of ["space-1", "space-2", "space-3", ""]) {
      const rows = sourcesInSpace(sources, spaceId);
      const usage = knowledgeUsage(sources, spaceId);
      expect(usage.files).toBe(rows.length);
      expect(usage.bytes).toBe(rows.reduce((sum, row) => sum + row.byte_size, 0));
    }
  });
});
