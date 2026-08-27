"use client";

import type { EmbeddingGeneration } from "@workspace/api-client";
import { ApiError } from "@workspace/api-client";
import { useEffect, useState } from "react";
import { api } from "../api";
import { describeError } from "./shared";

/**
 * How this deployment turns text into vectors, and how much of the corpus agrees.
 *
 * Retrieval has two arms — keyword and semantic — and the semantic one only
 * works if every vector it compares was produced the same way. A vector does not
 * carry that with it: two of the same width from different models score against
 * each other perfectly happily and return a plausible, wrong ranking. So the
 * deployment names one contract, and retrieval compares only within it.
 *
 * What this panel is for is watching a migration. Changing the contract does not
 * rewrite anything — it opens a new generation, `building`, which nothing reads
 * until its coverage is complete and someone activates it. Until then this is
 * where "how far along is it" gets answered, and afterwards it is the record of
 * which contract to roll back to.
 *
 * Read-only, deliberately. Activating a generation changes what the entire
 * corpus is searchable by, so it lives in `scripts/rebuild_embeddings.py` where
 * it is logged, reviewable, and hard to do by accident.
 */

export type RetrievalContractPanelProps = {
  setError: (message: string) => void;
};

/** What one vector of this shape costs on disk. */
function bytesPerVector(row: EmbeddingGeneration): number {
  return row.dimensions * (row.storage_dtype === "float16" ? 2 : 4);
}

export function RetrievalContractPanel({ setError }: RetrievalContractPanelProps) {
  const [rows, setRows] = useState<EmbeddingGeneration[] | null>(null);

  useEffect(() => {
    let live = true;
    void (async () => {
      try {
        const loaded = await api.listRetrievalContract();
        if (live) setRows(loaded);
      } catch (caught) {
        // A 403 is the ordinary case for a workspace owner who does not
        // administer the org, and it is not worth a banner — the panel simply is
        // not theirs, and it renders nothing. Anything else is a real failure.
        if (caught instanceof ApiError && caught.status === 403) {
          if (live) setRows([]);
          return;
        }
        setError(describeError(caught, "Could not load the retrieval contract"));
      }
    })();
    return () => {
      live = false;
    };
  }, [setError]);

  if (!rows || rows.length === 0) return null;

  const active = rows.find((row) => row.status === "active");
  const building = rows.filter((row) => row.status === "building");
  // `pending` counts rows sitting on *another* contract, which is the honest
  // measure of outstanding work. Rows nothing has ever embedded are reported
  // apart from it, because they are equally absent from every generation and
  // counting them would make a migration look permanently unfinished.
  const pending = (active?.coverage ?? []).reduce((sum, row) => sum + row.pending, 0);
  const covered = (active?.coverage ?? []).reduce((sum, row) => sum + row.covered, 0);

  return (
    <section className="admin-panel">
      <div className="panel-title">
        <div>
          <strong>Retrieval contract</strong>
          <span>
            {active ? `${active.model} · ${active.dimensions}d` : "none active"}
          </span>
        </div>
        <span className="admin-tag">{active?.storage_dtype ?? "—"}</span>
      </div>

      <p className="field-hint">
        Semantic search compares vectors only within one contract, so everything
        it ranks was produced by one model at one width. Changing the
        configuration does not rewrite anything — it opens a new generation that
        nothing reads until it is complete and activated.
      </p>

      <div className="admin-stats">
        <div>
          <strong>{covered.toLocaleString()}</strong>
          <span>vectors on the active contract</span>
        </div>
        <div>
          <strong>{pending.toLocaleString()}</strong>
          <span>still on an older one</span>
        </div>
        <div>
          <strong>{building.length}</strong>
          <span>
            {building.length === 1 ? "build in progress" : "builds in progress"}
          </span>
        </div>
      </div>

      <div className="admin-table-scroll">
        <table className="admin-table">
          <thead>
            <tr>
              <th scope="col">Status</th>
              <th scope="col">Model</th>
              <th scope="col">Width</th>
              <th scope="col">Stored as</th>
              <th scope="col">Floor</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id}>
                <td>
                  <span className="admin-tag">{row.status}</span>
                </td>
                <td>
                  <strong>{row.model}</strong>
                  {/* The revision only earns a line when it disagrees with the
                      model asked for — which is the day it starts mattering. */}
                  {row.revision && row.revision !== row.model && (
                    <span> · {row.revision}</span>
                  )}
                </td>
                <td>{row.dimensions}d</td>
                <td>
                  {/* What the width actually costs per vector: the whole argument
                      for a narrower one, and otherwise arithmetic the reader has
                      to do themselves. */}
                  {row.storage_dtype} · {bytesPerVector(row)} B
                </td>
                <td>{row.dense_floor.toFixed(3)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {pending > 0 && (
        <p className="admin-empty">
          A migration is in progress. Retrieval keeps using the active contract
          until the new one covers the whole corpus.
        </p>
      )}
    </section>
  );
}
