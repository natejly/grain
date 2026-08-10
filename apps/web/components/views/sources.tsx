"use client";

import { File, Library, Plus, Trash2, UploadCloud } from "lucide-react";
import type { Source } from "@workspace/api-client";
import { formatBytes, formatRelative, statusLabel } from "./shared";

export type SourcesViewProps = {
  sources: Source[];
  uploading: boolean;
  dragging: boolean;
  setDragging: (value: boolean) => void;
  uploadFiles: (files: FileList | File[]) => Promise<void>;
  removeSource: (source: Source) => Promise<void>;
  fileInputRef: React.RefObject<HTMLInputElement | null>;
};

export function SourcesView({
  sources,
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
          <strong>{uploading ? "Indexing your source…" : "Drop a source here"}</strong>
          <span>Markdown, text, PDF, CSV, or JSON · up to 10 MB</span>
        </div>
        <button type="button">{uploading ? "Working…" : "Browse"}</button>
      </div>

      <div className="library-summary">
        <div>
          <strong>{sources.length}</strong>
          <span>sources</span>
        </div>
        <div>
          <strong>{sources.reduce((sum, source) => sum + source.chunk_count, 0)}</strong>
          <span>indexed passages</span>
        </div>
        <div>
          <strong>{formatBytes(sources.reduce((sum, source) => sum + source.byte_size, 0))}</strong>
          <span>stored originals</span>
        </div>
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
            <Library size={24} />
            <strong>No sources yet</strong>
            <span>Add a document to make it searchable.</span>
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
              <button
                className="delete-button"
                onClick={() => void removeSource(source)}
                title="Delete source"
                aria-label={`Delete ${source.filename}`}
              >
                <Trash2 size={15} />
              </button>
            </div>
          ))
        )}
      </div>
    </section>
  );
}
