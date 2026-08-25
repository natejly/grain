"use client";

import { ExternalLink, File, Plus, Trash2, UploadCloud } from "lucide-react";
import type { KnowledgeGraph, Source, Space } from "@workspace/api-client";
import { useState } from "react";
import { api } from "../api";
import { describeError, formatBytes, formatRelative, statusLabel } from "./shared";
import { spaceNameForId } from "./space-threads";
import { useFocusReveal } from "./use-focus-reveal";

/**
 * Open a stored original in a new tab.
 *
 * A plain `<a href>` to the API cannot work: the route is authenticated, the
 * API is a different site from this app, and a top-level navigation carries no
 * `X-Workspace-Id` — so the browser would either be refused or handed whichever
 * workspace the user joined first. The bytes are fetched through the client and
 * opened as an object URL instead, which is same-origin and always the file the
 * row names.
 */
function OpenSourceButton({
  source,
  setError,
}: {
  source: Source;
  setError: (message: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <button
      className="open-button"
      disabled={busy}
      title="Open original"
      aria-label={`Open ${source.filename}`}
      onClick={() => {
        setBusy(true);
        void api
          .sourceContent(source.id)
          .then((blob) => {
            const url = URL.createObjectURL(blob);
            window.open(url, "_blank", "noopener");
            // The tab has to have read it first; revoking immediately hands it
            // a dead URL. A minute is far longer than any load and still bounds
            // the leak to one click.
            window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
          })
          .catch((caught) => setError(describeError(caught, "Could not open that file")))
          .finally(() => setBusy(false));
      }}
    >
      <ExternalLink size={14} />
    </button>
  );
}

export type SourcesViewProps = {
  sources: Source[];
  setError: (message: string) => void;
  uploading: boolean;
  dragging: boolean;
  setDragging: (value: boolean) => void;
  // Returns the uploaded Source (the attach popover chains a dataset on it);
  // this page fires and forgets, so the row is simply unused here.
  uploadFiles: (files: FileList | File[]) => Promise<unknown>;
  removeSource: (source: Source) => Promise<void>;
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  // The cross-link surface, optional so the view still stands alone (tests
  // mount it bare): the graph tells a row how far it reached, the spaces list
  // names its scope, and openEntity/focus are the two directions of travel.
  graph?: KnowledgeGraph | null;
  spaces?: Space[];
  openEntity?: (entityId: string) => void;
  focused?: string | null;
  setFocused?: (id: string | null) => void;
};

const noFocus = () => undefined;

export function SourcesView({
  sources,
  setError,
  uploading,
  dragging,
  setDragging,
  uploadFiles,
  removeSource,
  fileInputRef,
  graph = null,
  spaces = [],
  openEntity,
  focused = null,
  setFocused = noFocus,
}: SourcesViewProps) {
  useFocusReveal("source", focused, setFocused);
  const entities = graph?.entities ?? [];
  return (
    <section className="content-page">
      <div className="page-heading">
        <div>
          <h1>Sources</h1>
          <p>The files the agent quotes from — indexed into passages, projected into the graph.</p>
        </div>
        <button className="primary-button" onClick={() => fileInputRef.current?.click()}>
          <Plus size={16} />
          Add source
        </button>
      </div>

      <div
        className={`drop-zone ${dragging ? "dragging" : ""}`}
        onDragEnter={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragOver={(event) => event.preventDefault()}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          void uploadFiles(event.dataTransfer.files);
        }}
        onClick={() => fileInputRef.current?.click()}
      >
        <input
          ref={fileInputRef}
          type="file"
          accept=".txt,.md,.markdown,.pdf,.csv,.json"
          hidden
          onChange={(event) => event.target.files && void uploadFiles(event.target.files)}
        />
        <div className="upload-icon">
          <UploadCloud size={21} />
        </div>
        <div>
          <strong>{uploading ? "Indexing…" : "Drop a file"}</strong>
        </div>
        <button type="button">{uploading ? "Working…" : "Browse"}</button>
      </div>

      <div className="source-table">
        <div className="source-table-head">
          <span>Source</span>
          <span>Status</span>
          <span>Passages</span>
          <span>Added</span>
          <span />
        </div>
        {sources.length === 0 ? (
          <div className="table-empty">
            <strong>No sources yet</strong>
          </div>
        ) : (
          sources.map((source) => {
            // How far this file reached: the graph entities it projected into.
            // Zero is normal (stored figures, files still indexing), so the
            // link only appears when there is somewhere to land.
            const reach = entities.filter((entity) =>
              entity.source_ids.includes(source.id),
            );
            const spaceName = spaceNameForId(source.space_id, spaces);
            return (
            <div
              className={focused === source.id ? "source-row focused" : "source-row"}
              id={`source-${source.id}`}
              key={source.id}
            >
              <div className="source-name">
                <div className="file-icon">
                  <File size={17} />
                </div>
                <span>
                  <strong>
                    {source.filename}
                    {spaceName && <span className="source-space">{spaceName}</span>}
                  </strong>
                  <small>{formatBytes(source.byte_size)}</small>
                </span>
              </div>
              <div>
                <span className={`source-status ${source.status}`}>
                  <i />
                  {statusLabel(source.status)}
                </span>
                {source.error && <small className="source-error">{source.error}</small>}
              </div>
              <span className="muted-cell">
                {reach.length > 0 && openEntity ? (
                  <button
                    className="knowledge-link"
                    title="See this file's entities in the graph"
                    onClick={() => openEntity(reach[0].id)}
                  >
                    {source.chunk_count} passages · {reach.length}{" "}
                    {reach.length === 1 ? "entity" : "entities"}
                  </button>
                ) : (
                  source.chunk_count || "—"
                )}
              </span>
              <span className="muted-cell">{formatRelative(source.created_at)}</span>
              <div className="source-row-actions">
                <OpenSourceButton source={source} setError={setError} />
                <button
                  className="delete-button"
                  onClick={() => void removeSource(source)}
                  title="Delete source"
                  aria-label={`Delete ${source.filename}`}
                >
                  <Trash2 size={15} />
                </button>
              </div>
            </div>
            );
          })
        )}
      </div>
    </section>
  );
}
