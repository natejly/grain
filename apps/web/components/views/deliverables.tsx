"use client";

import type {
  DeliverableManifest,
  DeliverableManifestDetail,
  Space,
} from "@workspace/api-client";
import { PackageCheck, Play } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import { api } from "../api";
import { NO_COVERAGE, summarizeCoverage } from "./coverage-format";
import { describeManifestStatus, describeSpend } from "./deliverable-format";
import { describeActionError, describeError, formatRelative } from "./shared";

/**
 * Deliverable runs, and the manifest each one leaves behind.
 *
 * The manifest's entire value is the JOIN — this file came out of that sandbox
 * session, and the run that made it was holding these queries and these cited
 * passages — so the detail view is where that join becomes visible.
 *
 * ONE THING THIS SCREEN MUST NOT DO: offer to cite a manifest file.
 * `sandbox_download` writes its Source rows with status "stored", not "ready",
 * precisely so retrieval cannot quote them. A "cite this" affordance here
 * would have the product claiming provenance it does not have.
 */
export type DeliverablesViewProps = {
  setError: (message: string) => void;
};

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function DeliverablesView({ setError }: DeliverablesViewProps) {
  const [manifests, setManifests] = useState<DeliverableManifest[]>([]);
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [activeId, setActiveId] = useState("");
  const [detail, setDetail] = useState<DeliverableManifestDetail | null>(null);
  const [composing, setComposing] = useState(false);
  const [title, setTitle] = useState("");
  const [question, setQuestion] = useState("");
  const [spaceId, setSpaceId] = useState("");
  const [budgetSeconds, setBudgetSeconds] = useState("900");
  const [busy, setBusy] = useState(false);
  const [started, setStarted] = useState("");

  const load = useCallback(async () => {
    try {
      const [rows, spaceRows] = await Promise.all([
        api.listDeliverables(),
        api.listSpaces().catch(() => [] as Space[]),
      ]);
      setManifests(rows);
      setSpaces(spaceRows);
    } catch (caught) {
      setError(describeError(caught, "Could not load deliverables"));
    } finally {
      setLoaded(true);
    }
  }, [setError]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!activeId) {
      setDetail(null);
      return;
    }
    let live = true;
    void api
      .getDeliverable(activeId)
      .then((found) => {
        if (live) setDetail(found);
      })
      .catch((caught) =>
        setError(describeError(caught, "Could not open that manifest")),
      );
    return () => {
      live = false;
    };
  }, [activeId, setError]);

  async function start() {
    if (!question.trim() || busy) return;
    setBusy(true);
    setStarted("");
    try {
      const result = await api.startDeliverable({
        title: title.trim(),
        question: question.trim(),
        space_id: spaceId,
        budget_seconds: Number(budgetSeconds) || undefined,
      });
      setStarted(
        `Started. Watch it on the Workflows page — run ${result.workflow_run_id}.`,
      );
      setComposing(false);
    } catch (caught) {
      setError(describeActionError(caught, "Could not start that deliverable run"));
    } finally {
      setBusy(false);
    }
  }

  function spaceName(id: string): string {
    if (!id) return "Workspace library";
    return spaces.find((space) => space.id === id)?.name ?? id;
  }

  return (
    <div className="workflow-layout">
      <aside className="workflow-sidebar">
        <div className="workflow-sidebar-head">
          <span>Deliverables</span>
          <button
            className="icon-button"
            aria-label="Start a deliverable run"
            onClick={() => {
              setComposing(true);
              setActiveId("");
            }}
          >
            <Play size={16} />
          </button>
        </div>
        {manifests.length === 0 ? (
          <p className="workflow-empty">
            {loaded ? "No deliverable runs yet." : "Loading…"}
          </p>
        ) : (
          <ul className="workflow-items">
            {manifests.map((manifest) => (
              <li key={manifest.id}>
                <button
                  type="button"
                  className={
                    manifest.id === activeId ? "workflow-item active" : "workflow-item"
                  }
                  onClick={() => {
                    setComposing(false);
                    setActiveId(manifest.id);
                  }}
                >
                  <span className="workflow-item-name">
                    <PackageCheck size={14} />
                    {manifest.title || "Untitled"}
                  </span>
                  <span className="workflow-item-meta">
                    {spaceName(manifest.space_id)} ·{" "}
                    {formatRelative(manifest.created_at)}
                    {manifest.status === "partial" && (
                      <span className="page-drift-chip">partial</span>
                    )}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </aside>

      <section className="workflow-detail">
        {composing ? (
          <section className="workflow-author">
            <div className="page-heading">
              <div>
                <h1>Start a deliverable run</h1>
              </div>
            </div>
            <form
              className="cron-form"
              onSubmit={(event) => {
                event.preventDefault();
                void start();
              }}
            >
              <label className="cron-field">
                <span>Title</span>
                <input
                  value={title}
                  onChange={(event) => setTitle(event.target.value)}
                  placeholder="Q3 retention review"
                />
              </label>
              <label className="cron-field">
                <span>What should it answer?</span>
                <textarea
                  value={question}
                  onChange={(event) => setQuestion(event.target.value)}
                  rows={3}
                  placeholder="How has our retention policy changed, and what does it cost?"
                />
              </label>
              <label className="cron-field">
                <span>Space</span>
                <select
                  value={spaceId}
                  onChange={(event) => setSpaceId(event.target.value)}
                >
                  <option value="">Workspace library</option>
                  {spaces.map((space) => (
                    <option key={space.id} value={space.id}>
                      {space.name}
                    </option>
                  ))}
                </select>
              </label>
              <label className="cron-field">
                <span>Time budget (seconds)</span>
                <input
                  value={budgetSeconds}
                  onChange={(event) => setBudgetSeconds(event.target.value)}
                  inputMode="numeric"
                />
                {/* A run that exhausts its budget still ships a manifest,
                    marked partial, with whatever landed. */}
                <span className="cron-zone-note">
                  A run that runs out still ships what it made, marked partial.
                </span>
              </label>
              <div className="cron-form-actions">
                <button
                  type="submit"
                  className="primary-button"
                  disabled={!question.trim() || busy}
                >
                  {busy ? "Starting…" : "Start run"}
                </button>
                <button
                  type="button"
                  className="ghost-button"
                  onClick={() => setComposing(false)}
                >
                  Cancel
                </button>
              </div>
            </form>
          </section>
        ) : !detail ? (
          <p className="workflow-empty">
            {started || "Select a manifest."}
          </p>
        ) : (
          <>
            <div className="page-heading">
              <div>
                <h1>{detail.manifest.title || "Untitled"}</h1>
                <p className="page-subtitle">
                  {spaceName(detail.manifest.space_id)} ·{" "}
                  {describeManifestStatus(detail.manifest)} ·{" "}
                  {describeSpend(detail.manifest)}
                </p>
              </div>
            </div>

            <section className="page-citations">
              <h2>Files</h2>
              {detail.files.length === 0 ? (
                <p className="workflow-empty">This run produced no files.</p>
              ) : (
                <ol>
                  {detail.files.map((file) => (
                    <li key={`${file.ordinal}-${file.source_id}`}>
                      <span className="page-citation-head">
                        <strong>{file.filename || "unnamed"}</strong>{" "}
                        {formatBytes(file.byte_size)}
                      </span>
                      <details>
                        <summary>Derived from</summary>
                        <p>Sandbox session {file.sandbox_session_id || "—"}</p>
                        {file.queries.length > 0 && (
                          <ul>
                            {file.queries.map((query) => (
                              <li key={query}>
                                <code>{query}</code>
                              </li>
                            ))}
                          </ul>
                        )}
                        <p className="cron-zone-note">
                          {file.chunk_ids.length} cited passage
                          {file.chunk_ids.length === 1 ? "" : "s"} behind this run.
                          Stored output files are not indexed, so they cannot be
                          cited back.
                        </p>
                      </details>
                    </li>
                  ))}
                </ol>
              )}
            </section>

            <section className="coverage-ledger-section">
              <h2>Coverage</h2>
              {detail.coverage === null ? (
                <p className="workflow-empty">{NO_COVERAGE}</p>
              ) : (
                <details className="coverage-ledger">
                  <summary>{summarizeCoverage(detail.coverage)}</summary>
                  <ReactMarkdown>{detail.coverage.report_markdown}</ReactMarkdown>
                </details>
              )}
            </section>
          </>
        )}
      </section>
    </div>
  );
}
