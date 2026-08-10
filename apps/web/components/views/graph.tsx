"use client";

import { Network, RefreshCw, Trash2 } from "lucide-react";
import type { KnowledgeGraph, MemoryItem } from "@workspace/api-client";
import { useState } from "react";
import { Graph3D } from "../graph-3d";
import { formatRelative } from "./shared";

export type GraphViewProps = {
  graph: KnowledgeGraph | null;
  memories: MemoryItem[];
  rebuild: () => Promise<void>;
  openChunk: (chunkId: string) => Promise<void>;
  forgetMemory: (item: MemoryItem) => Promise<void>;
};

export function GraphView({ graph, memories, rebuild, openChunk, forgetMemory }: GraphViewProps) {
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
        <div>
          <strong>{memories.length}</strong>
          <span>long-term memories</span>
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

      <div className="memory-panel">
        <div className="panel-title">
          <div>
            <strong>Long-term memory</strong>
            <p>Recalled automatically in chat.</p>
          </div>
          <span className="panel-count">{memories.length}</span>
        </div>
        {memories.length === 0 ? (
          <div className="feature-empty">
            <Network size={20} />
            <strong>No memories yet</strong>
            <span>Chat with the assistant and durable facts will accumulate here.</span>
          </div>
        ) : (
          <div className="memory-list">
            {memories.map((item) => (
              <div className="memory-row" key={item.id}>
                <div>
                  <span className={`memory-kind ${item.kind}`}>
                    {item.kind.replaceAll("_", " ")}
                  </span>
                  <p>{item.content}</p>
                  <small>
                    {item.importance > 1 && `reinforced ×${item.importance} · `}
                    updated {formatRelative(item.updated_at)}
                  </small>
                </div>
                <button
                  className="delete-button"
                  title="Forget this memory"
                  onClick={() => void forgetMemory(item)}
                >
                  <Trash2 size={14} />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}
