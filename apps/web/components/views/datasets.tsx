"use client";

import type { Dataset, DatasetQueryResult, Source } from "@workspace/api-client";
import { BarChart3, Plus, RefreshCw, Table2 } from "lucide-react";
import { useState } from "react";
import { api } from "../api";
import { describeError, formatRelative, isTabular } from "./shared";

/**
 * Datasets, as a place. They have existed since ADR 0003 — immutable versions,
 * bounded DuckDB queries, the thing every dashboard is bound to — but had no
 * page: they were created as a side effect of uploading a CSV and appeared
 * only inside the app editor's binding chips. A user asking "what data does
 * this workspace actually hold, and what shape is it" had nowhere to stand.
 *
 * What is deliberately NOT here: delete. Versions are immutable and dashboards
 * bind to them by id — the honest offer is a new version, not an eraser. And
 * no free-form SQL: the preview runs the same typed query contract the tiles
 * run, because "bounded analytics" is a product gate, not a UI shortage.
 */
export type DatasetsViewProps = {
  datasets: Dataset[];
  /** Indexed tabular sources — what a new dataset (or version) is made from. */
  sources: Source[];
  createDataset: (name: string, sourceId: string) => Promise<void>;
  createVersion: (datasetId: string, sourceId: string) => Promise<void>;
  /**
   * The cross-link that closes the connect-data → chart-it trek: prefills the
   * chat composer with a chart ask about this dataset and goes there. The
   * agent writes dashboards; this page hands it the sentence.
   */
  chartThis: (dataset: Dataset) => void;
  setError: (message: string) => void;
};

function TabularSourcePicker({
  sources,
  value,
  onChange,
  label,
}: {
  sources: Source[];
  value: string;
  onChange: (sourceId: string) => void;
  label: string;
}) {
  const tabular = sources.filter(
    (source) => source.status === "ready" && isTabular(source.filename),
  );
  return (
    <select
      value={value}
      onChange={(event) => onChange(event.target.value)}
      aria-label={label}
    >
      <option value="">Pick an indexed CSV or JSON…</option>
      {tabular.map((source) => (
        <option key={source.id} value={source.id}>
          {source.filename}
        </option>
      ))}
    </select>
  );
}

export function DatasetsView({
  datasets,
  sources,
  createDataset,
  createVersion,
  chartThis,
  setError,
}: DatasetsViewProps) {
  const [openId, setOpenId] = useState("");
  const [preview, setPreview] = useState<DatasetQueryResult | null>(null);
  const [previewFor, setPreviewFor] = useState("");
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [newSource, setNewSource] = useState("");
  const [versionSource, setVersionSource] = useState("");
  const [busy, setBusy] = useState(false);

  const open = datasets.find((item) => item.id === openId) ?? null;

  async function loadPreview(dataset: Dataset) {
    setPreviewFor(dataset.id);
    setPreview(null);
    try {
      // The same typed contract the dashboard tiles run — a look at the top of
      // the table, not a query surface.
      setPreview(await api.queryDataset(dataset.id, { limit: 20 }));
    } catch (caught) {
      setError(describeError(caught, "Could not preview the dataset"));
    }
  }

  function select(dataset: Dataset) {
    setOpenId(dataset.id);
    setVersionSource("");
    void loadPreview(dataset);
  }

  async function submitCreate() {
    if (!newName.trim() || !newSource || busy) return;
    setBusy(true);
    try {
      await createDataset(newName.trim(), newSource);
      setCreating(false);
      setNewName("");
      setNewSource("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="content-page datasets-page">
      <div className="page-heading">
        <div>
          <h1>Datasets</h1>
          <p>
            Immutable table versions the agent queries and dashboards bind to.
            Upload a CSV in chat or Sources and it can become one.
          </p>
        </div>
        <div className="page-heading-actions">
          <button className="primary-button" onClick={() => setCreating((value) => !value)}>
            <Plus size={16} />
            New dataset
          </button>
        </div>
      </div>

      {creating && (
        <form
          className="dataset-create"
          aria-label="New dataset"
          onSubmit={(event) => {
            event.preventDefault();
            void submitCreate();
          }}
        >
          <input
            value={newName}
            placeholder="Dataset name"
            aria-label="Dataset name"
            autoFocus
            onChange={(event) => setNewName(event.target.value)}
          />
          <TabularSourcePicker
            sources={sources}
            value={newSource}
            onChange={setNewSource}
            label="Source file"
          />
          <button
            type="submit"
            className="primary-button"
            disabled={busy || !newName.trim() || !newSource}
          >
            {busy ? "Creating…" : "Create dataset"}
          </button>
        </form>
      )}

      {datasets.length === 0 ? (
        <div className="empty-state">
          <p>
            No datasets yet. Attach a CSV in chat — the popover offers to make
            one — or create one here from an indexed source.
          </p>
        </div>
      ) : (
        <div className="dataset-layout">
          <ul className="dataset-list" aria-label="Datasets">
            {datasets.map((dataset) => (
              <li key={dataset.id}>
                <button
                  className={dataset.id === openId ? "dataset-row active" : "dataset-row"}
                  onClick={() => select(dataset)}
                >
                  <Table2 size={14} aria-hidden="true" />
                  <span className="dataset-row-name">{dataset.name}</span>
                  <span className="dataset-row-meta">
                    {dataset.row_count.toLocaleString()} rows · v{dataset.current_version}
                  </span>
                </button>
              </li>
            ))}
          </ul>

          {open ? (
            <div className="dataset-detail">
              <header className="dataset-detail-head">
                <div>
                  <h2>{open.name}</h2>
                  <span>
                    {open.format.toUpperCase()} · {open.row_count.toLocaleString()} rows ·
                    version {open.current_version} · updated {formatRelative(open.updated_at)}
                  </span>
                </div>
                <button className="primary-button" onClick={() => chartThis(open)}>
                  <BarChart3 size={14} />
                  Chart this
                </button>
              </header>

              <div className="dataset-columns" aria-label="Columns">
                {open.columns.map((column) => (
                  <span key={column.name} className="dataset-column-chip">
                    {column.name}
                    <small>{column.type}</small>
                  </span>
                ))}
              </div>

              {previewFor === open.id && preview && (
                <div className="dataset-preview">
                  <div className="dataset-preview-bar">
                    <span>
                      First {preview.rows.length} of {preview.row_count.toLocaleString()} rows
                    </span>
                    <button
                      className="icon-button"
                      aria-label="Reload preview"
                      onClick={() => void loadPreview(open)}
                    >
                      <RefreshCw size={13} />
                    </button>
                  </div>
                  <div className="dataset-table-scroll">
                    <table>
                      <thead>
                        <tr>
                          {preview.columns.map((column) => (
                            <th key={column} scope="col">
                              {column}
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {preview.rows.map((row, index) => (
                          <tr key={index}>
                            {preview.columns.map((column) => (
                              <td key={column}>{String(row[column] ?? "")}</td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}

              <form
                className="dataset-version"
                aria-label="New version"
                onSubmit={(event) => {
                  event.preventDefault();
                  if (!versionSource || busy) return;
                  setBusy(true);
                  void createVersion(open.id, versionSource)
                    .then(() => setVersionSource(""))
                    .finally(() => setBusy(false));
                }}
              >
                <span>
                  New version from a fresh file — the old version stays, and
                  what was built on it keeps working:
                </span>
                <TabularSourcePicker
                  sources={sources}
                  value={versionSource}
                  onChange={setVersionSource}
                  label="Version source file"
                />
                <button
                  type="submit"
                  className="ghost-button"
                  disabled={busy || !versionSource}
                >
                  Create version
                </button>
              </form>
            </div>
          ) : (
            <div className="dataset-detail empty">
              <p>Pick a dataset to see its columns and a preview.</p>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
