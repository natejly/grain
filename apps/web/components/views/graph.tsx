"use client";

import { Network, RefreshCw } from "lucide-react";
import type { KnowledgeGraph } from "@workspace/api-client";
import { useState } from "react";
import { Graph3D } from "../graph-3d";
import { formatRelative } from "./shared";

export type GraphViewProps = {
  graph: KnowledgeGraph | null;
  rebuild: () => Promise<void>;
  openChunk: (chunkId: string) => Promise<void>;
};

/**
 * The projection over indexed sources. Long-term memory used to hang off the
 * bottom of this page; it is its own surface now (views/memory.tsx), and an
 * entity that came from a memory still says so in its row.
 */
export function GraphView({ graph, rebuild, openChunk }: GraphViewProps) {
  const [selected, setSelected] = useState<string | null>(null);
  // No slice and no hand-placed ring: the force simulation lays out whatever the
  // API returns, so the cap belongs to the query, not the renderer.
  const nodes = graph?.entities || [];
  const known = new Set(nodes.map((node) => node.id));
  const edges = (graph?.edges || []).filter(
    (edge) => known.has(edge.from_entity_id) && known.has(edge.to_entity_id),
  );

  return (
    <section className="content-page graph-page">
      <div className="page-heading">
        <div>
          <h1>Knowledge graph</h1>
        </div>
        <button
          className="secondary-button"
          onClick={() => void rebuild()}
          disabled={graph?.status === "queued" || graph?.status === "building"}
        >
          <RefreshCw
            size={15}
            className={graph?.status === "queued" || graph?.status === "building" ? "spin" : ""}
          />
          Rebuild
        </button>
      </div>

      <div className="graph-summary">
        <div>
          <strong>{graph?.entities.length || 0}</strong>
          <span>entities shown</span>
        </div>
        <div>
          <strong>{graph?.edges.length || 0}</strong>
          <span>links shown</span>
        </div>
        {graph?.built_at && (
          <div>
            <strong>{graph.status}</strong>
            <span>built {formatRelative(graph.built_at)}</span>
          </div>
        )}
      </div>

      {nodes.length === 0 ? (
        <div className="feature-empty">
          <Network size={25} />
          <strong>No graph projection yet</strong>
          <span>Index a source or rebuild the projection.</span>
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
