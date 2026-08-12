"use client";

import { RefreshCw } from "lucide-react";
import type { KnowledgeGraph } from "@workspace/api-client";
import { useEffect, useRef, useState } from "react";
import { Graph3D } from "../graph-3d";

export type GraphViewProps = {
  graph: KnowledgeGraph | null;
  rebuild: () => Promise<void>;
  openChunk: (chunkId: string) => Promise<void>;
};

/**
 * The projection over indexed sources *and* long-term memory. Memory used to
 * hang off the bottom of this page; it is its own surface now
 * (views/memory.tsx), and an entity that came from a memory still says so in
 * its row.
 */
export function GraphView({ graph, rebuild, openChunk }: GraphViewProps) {
  const [selected, setSelected] = useState<string | null>(null);
  const asked = useRef(false);
  // Every memory write marks the projection stale, and until this nothing read
  // that status back: a rebuild only ever ran from source ingest, source
  // delete, or this page's button. A workspace that learns from conversation
  // and has uploaded no documents therefore sat at stale/never-built forever —
  // it had entities to show and no path that would ever project them.
  //
  // Repaired here rather than on GET /api/graph because refreshSecondary re-reads
  // the graph after every chat turn, and a rebuild is up to 60 extraction calls;
  // paying that per turn to serve a page nobody opened is the wrong trade. This
  // component mounts only when the graph tab is open, so the cost is one rebuild
  // per visit that finds the projection out of date.
  useEffect(() => {
    if (graph?.status !== "stale" || asked.current) return;
    asked.current = true;
    void rebuild();
  }, [graph?.status, rebuild]);
  // No slice and no hand-placed ring: the force simulation lays out whatever the
  // API returns, so the cap belongs to the query, not the renderer.
  const nodes = graph?.entities || [];
  const known = new Set(nodes.map((node) => node.id));
  const edges = (graph?.edges || []).filter(
    (edge) => known.has(edge.from_entity_id) && known.has(edge.to_entity_id),
  );
  // Server-confirmed in flight. `stale` is deliberately not included: if the
  // automatic rebuild above failed the status stays stale, and disabling the
  // button on it would leave the user no way to retry the thing that broke.
  const rebuilding = graph?.status === "queued" || graph?.status === "building";
  // For the empty state, though, `stale` is "about to build" — the effect has
  // already asked — and saying "No graph yet" in that window answers a question
  // we have not finished asking.
  const pending = rebuilding || graph?.status === "stale";

  return (
    <section className="content-page graph-page">
      <div className="page-heading">
        <div>
          <h1>Knowledge graph</h1>
        </div>
        <button className="secondary-button" onClick={() => void rebuild()} disabled={rebuilding}>
          <RefreshCw size={15} className={rebuilding ? "spin" : ""} />
          Rebuild
        </button>
      </div>

      {nodes.length === 0 ? (
        <div className="feature-empty">
          {pending ? (
            <>
              <strong>Building the graph</strong>
              <span>Projecting entities from your sources and memories.</span>
            </>
          ) : (
            <>
              <strong>No graph yet</strong>
              <span>Index a source or save a memory, then rebuild.</span>
            </>
          )}
        </div>
      ) : (
        <div className="graph-layout">
          <div className="graph-canvas">
            <Graph3D
              entities={nodes}
              edges={edges}
              onSelect={(entity) => setSelected(entity?.id ?? null)}
            />
          </div>
          <div className="entity-list">
            <div className="panel-title">
              <strong>Entities</strong>
              <span className="panel-count">{graph?.entities.length || 0}</span>
            </div>
            {nodes.map((entity) => (
              <div
                className={
                  selected === entity.id ? "entity-row selected" : "entity-row"
                }
                key={entity.id}
              >
                <div>
                  <strong>{entity.name}</strong>
                  <span>
                    {entity.entity_type.replaceAll("_", " ")} · {entity.mention_count} mentions
                    {entity.memory_ids.length > 0 && " · from memory"}
                  </span>
                </div>
                {entity.chunk_ids[0] && (
                  <button onClick={() => void openChunk(entity.chunk_ids[0])}>Passage</button>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}
