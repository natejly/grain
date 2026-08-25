"use client";

import { RefreshCw } from "lucide-react";
import type { KnowledgeGraph, Source } from "@workspace/api-client";
import { useEffect, useRef, useState } from "react";
import { Graph3D } from "../graph-3d";
import { useFocusReveal } from "./use-focus-reveal";

export type GraphViewProps = {
  graph: KnowledgeGraph | null;
  rebuild: () => Promise<void>;
  openChunk: (chunkId: string) => Promise<void>;
  // The cross-link surface, optional so the view still stands alone (tests
  // mount it with exactly the three above): sources name where an entity was
  // read from, and the two open* callbacks walk back to the inputs — the file
  // rows and the memory rows this projection was built over.
  sources?: Source[];
  openSource?: (sourceId: string) => void;
  openMemory?: (memoryId: string) => void;
  focused?: string | null;
  setFocused?: (id: string | null) => void;
};

const noFocus = () => undefined;

/**
 * The projection over indexed sources *and* long-term memory. Memory used to
 * hang off the bottom of this page; it is its own surface now
 * (views/memory.tsx), and an entity that came from a memory still says so in
 * its row.
 */
export function GraphView({
  graph,
  rebuild,
  openChunk,
  sources = [],
  openSource,
  openMemory,
  focused = null,
  setFocused = noFocus,
}: GraphViewProps) {
  const [selected, setSelected] = useState<string | null>(null);
  useFocusReveal("entity", focused, setFocused);
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
          <h1>Graph</h1>
          <p>One projection over your sources and memories — what they mention, connected.</p>
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
            {nodes.map((entity) => {
              // Resolved rows only: an id whose source was deleted since the
              // last rebuild names nothing the Sources page could land on.
              const from = entity.source_ids
                .map((id) => sources.find((source) => source.id === id))
                .filter((source): source is Source => Boolean(source));
              return (
              <div
                className={[
                  "entity-row",
                  selected === entity.id ? "selected" : "",
                  focused === entity.id ? "focused" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                id={`entity-${entity.id}`}
                key={entity.id}
              >
                <div>
                  <strong>{entity.name}</strong>
                  <span>
                    {entity.entity_type.replaceAll("_", " ")} · {entity.mention_count} mentions
                    {entity.memory_ids.length > 0 &&
                      (openMemory ? (
                        <>
                          {" · "}
                          <button
                            className="knowledge-link"
                            title="Show the memories behind this"
                            onClick={() => openMemory(entity.memory_ids[0])}
                          >
                            from {entity.memory_ids.length}{" "}
                            {entity.memory_ids.length === 1 ? "memory" : "memories"}
                          </button>
                        </>
                      ) : (
                        " · from memory"
                      ))}
                  </span>
                </div>
                {from.length > 0 && openSource && (
                  <button
                    className="entity-source-chip"
                    title={`Show ${from[0].filename} in Sources`}
                    onClick={() => openSource(from[0].id)}
                  >
                    {from[0].filename}
                    {from.length > 1 && ` +${from.length - 1}`}
                  </button>
                )}
                {entity.chunk_ids[0] && (
                  <button onClick={() => void openChunk(entity.chunk_ids[0])}>Passage</button>
                )}
              </div>
              );
            })}
          </div>
        </div>
      )}
    </section>
  );
}
