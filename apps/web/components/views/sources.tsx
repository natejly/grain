"use client";

import { ExternalLink, File, Plus, Trash2, UploadCloud } from "lucide-react";
import type { Source } from "@workspace/api-client";
import { useState } from "react";
import { api } from "../api";
import { describeError, formatBytes, formatRelative, statusLabel } from "./shared";

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
};

export function SourcesView({
  sources,
  setError,
  uploading,
  dragging,
  setDragging,
  uploadFiles,
  removeSource,
  fileInputRef,
}: SourcesViewProps) {
  return (
    <section className="content-page">
      <div className="page-heading">
        <div>
          <h1>Sources</h1>
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
          sources.map((source) => (
            <div className="source-row" key={source.id}>
              <div className="source-name">
                <div className="file-icon">
                  <File size={17} />
                </div>
                <span>
                  <strong>{source.filename}</strong>
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
              <span className="muted-cell">{source.chunk_count || "—"}</span>
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
          ))
        )}
      </div>
    </section>
  );
}
