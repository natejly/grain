import type { Source } from "@workspace/api-client";
import { sourcesInSpace } from "./space-threads";

/**
 * The knowledge shelf's capacity arithmetic — pure, DOM-free, exhaustively
 * testable, in the folder-tree.ts / space-threads.ts mould.
 */

/**
 * The soft ceiling the meter draws its percent against. Purely cosmetic
 * configuration: nothing is enforced anywhere — changing this changes only
 * how full the bar looks, never what an upload is allowed to do.
 */
export const SPACE_KNOWLEDGE_SOFT_CEILING_BYTES = 50 * 1024 * 1024;

export type KnowledgeUsage = {
  files: number;
  bytes: number;
  /** 0–100, capped: a shelf past the soft ceiling reads full, not 130%. */
  percent: number;
};

/**
 * How much one space's shelf holds. Built on `sourcesInSpace`, so the meter
 * sums exactly the rows the list beside it shows and the two can never
 * disagree — and so `spaceId === ""` returns zeros: "" is the wire spelling
 * of the workspace library, and the sentinel must never over-match it.
 */
export function knowledgeUsage(sources: Source[], spaceId: string): KnowledgeUsage {
  const rows = sourcesInSpace(sources, spaceId);
  const bytes = rows.reduce((sum, source) => sum + source.byte_size, 0);
  return {
    files: rows.length,
    bytes,
    percent: Math.min(
      100,
      Math.round((bytes / SPACE_KNOWLEDGE_SOFT_CEILING_BYTES) * 100),
    ),
  };
}
