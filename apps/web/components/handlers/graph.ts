"use client";

import type {
  Citation,
  KnowledgeGraph,
  MemoryItem,
  ProvenanceChunk,
} from "@workspace/api-client";
import type { Dispatch, SetStateAction } from "react";
import { api } from "../api";
import { describeError } from "../views/shared";

/** Graph and memory actions, plus the provenance drawer both they and chat open. */
export type GraphHandlerDeps = {
  setError: Dispatch<SetStateAction<string>>;
  setGraph: Dispatch<SetStateAction<KnowledgeGraph | null>>;
  setMemories: Dispatch<SetStateAction<MemoryItem[]>>;
  setProvenance: Dispatch<SetStateAction<ProvenanceChunk | null>>;
  setLoadingProvenance: Dispatch<SetStateAction<boolean>>;
  refreshSecondary: () => Promise<void>;
};

export function createGraphHandlers({
  setError,
  setGraph,
  setMemories,
  setProvenance,
  setLoadingProvenance,
  refreshSecondary,
}: GraphHandlerDeps) {
  async function openChunk(chunkId: string) {
    setLoadingProvenance(true);
    setError("");
    try {
      setProvenance(await api.getChunk(chunkId));
    } catch (caught) {
      setError(describeError(caught, "Could not load provenance"));
    } finally {
      setLoadingProvenance(false);
    }
  }

  async function openCitation(citation: Citation) {
    await openChunk(citation.chunk_id);
  }

  async function rebuildKnowledgeGraph() {
    setError("");
    try {
      setGraph(await api.rebuildGraph());
      for (let attempt = 0; attempt < 30; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 400));
        const next = await api.getGraph();
        setGraph(next);
        if (!["queued", "building"].includes(next.status)) break;
      }
      await refreshSecondary();
    } catch (caught) {
      setError(describeError(caught, "Could not rebuild graph"));
    }
  }

  async function forgetMemory(item: MemoryItem) {
    if (!window.confirm("Forget this memory? It will not be recalled again.")) return;
    setError("");
    try {
      await api.deleteMemory(item.id);
      setMemories((items) => items.filter((memory) => memory.id !== item.id));
    } catch (caught) {
      setError(describeError(caught, "Could not forget memory"));
    }
  }

  return { openChunk, openCitation, rebuildKnowledgeGraph, forgetMemory };
}
