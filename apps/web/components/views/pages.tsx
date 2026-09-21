"use client";

import type { Page, PageDetail } from "@workspace/api-client";
import {
  BookOpen,
  CircleAlert,
  CircleCheck,
  CircleHelp,
  RefreshCw,
  Share2,
  Trash2,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkMath from "remark-math";
import { api } from "../api";
import { ShareLinksModal } from "../share-links-modal";
import { describeActionError, describeError, formatRelative } from "./shared";

/**
 * Pages: a thread published as a document whose citations are frozen.
 *
 * The one new idea on this screen is the per-citation STATUS PIP. A page
 * claims that following a marker shows the passage the answer was written
 * from; the pip is what makes that claim inspectable rather than asserted —
 * frozen, changed, or missing, per marker, from the sweep's own verdict.
 *
 * Everything else is borrowed on purpose: the document renderer, the share
 * modal, the two-pane list/detail shape Documents already uses.
 */
export type PagesViewProps = {
  setError: (message: string) => void;
};

function StatusPip({ status }: { status: string }) {
  const label =
    status === "changed"
      ? "The cited passage has been edited since this page was published"
      : status === "missing"
        ? // Two ways to hold no passage, and the copy has to cover both: the
          // chunk was deleted, or the page carries more citations than a page
          // freezes (the marker is kept either way, so the row is never
          // dropped out from under it).
          "No pinned passage — the source is gone, or this page has more citations than it freezes"
        : "Unchanged since publication";
  const Icon =
    status === "changed" ? CircleAlert : status === "missing" ? CircleHelp : CircleCheck;
  return (
    <span className="page-citation" data-status={status} title={label}>
      <Icon size={12} />
      {status}
    </span>
  );
}

export function PagesView({ setError }: PagesViewProps) {
  const [pages, setPages] = useState<Page[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [activeId, setActiveId] = useState("");
  const [detail, setDetail] = useState<PageDetail | null>(null);
  const [busy, setBusy] = useState(false);
  const [sharing, setSharing] = useState<Page | null>(null);

  const load = useCallback(async () => {
    try {
      setPages(await api.listPages());
    } catch (caught) {
      setError(describeError(caught, "Could not load pages"));
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
      .getPage(activeId)
      .then((found) => {
        if (live) setDetail(found);
      })
      .catch((caught) => setError(describeError(caught, "Could not open that page")));
    return () => {
      live = false;
    };
  }, [activeId, setError]);

  async function revalidate() {
    if (!detail || busy) return;
    setBusy(true);
    try {
      const updated = await api.revalidatePage(detail.page.id);
      setDetail(updated);
      setPages((rows) =>
        rows.map((row) => (row.id === updated.page.id ? updated.page : row)),
      );
    } catch (caught) {
      setError(describeActionError(caught, "Could not re-check that page"));
    } finally {
      setBusy(false);
    }
  }

  async function remove(page: Page) {
    if (
      !window.confirm(
        `Delete “${page.title}”? Any share links for it will stop working.`,
      )
    ) {
      return;
    }
    try {
      await api.deletePage(page.id);
      setPages((rows) => rows.filter((row) => row.id !== page.id));
      if (activeId === page.id) setActiveId("");
    } catch (caught) {
      setError(describeActionError(caught, "Could not delete that page"));
    }
  }

  return (
    <div className="workflow-layout">
      <aside className="workflow-sidebar">
        <div className="workflow-sidebar-head">
          <span>Pages</span>
        </div>
        {pages.length === 0 ? (
          <p className="workflow-empty">
            {loaded
              ? "No pages yet. Publish a thread from its header to freeze its citations."
              : "Loading…"}
          </p>
        ) : (
          <ul className="workflow-items">
            {pages.map((page) => (
              <li key={page.id}>
                <button
                  type="button"
                  className={page.id === activeId ? "workflow-item active" : "workflow-item"}
                  onClick={() => setActiveId(page.id)}
                >
                  <span className="workflow-item-name">
                    <BookOpen size={14} />
                    {page.title}
                  </span>
                  <span className="workflow-item-meta">
                    {formatRelative(page.created_at)}
                    {page.status === "drifted" && (
                      <span className="page-drift-chip">
                        {page.drift_count} changed
                      </span>
                    )}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </aside>

      <section className="workflow-detail">
        {!detail ? (
          <p className="workflow-empty">Select a page.</p>
        ) : (
          <>
            <div className="page-heading">
              <div>
                <h1>{detail.page.title}</h1>
                <p className="page-subtitle">
                  Published {formatRelative(detail.page.created_at)} ·{" "}
                  {detail.citations.length} frozen citation
                  {detail.citations.length === 1 ? "" : "s"}
                </p>
              </div>
              <div className="page-actions">
                <button
                  type="button"
                  className="ghost-button"
                  aria-disabled={busy}
                  onClick={() => void revalidate()}
                >
                  <RefreshCw size={14} />
                  {busy ? "Checking…" : "Re-check evidence"}
                </button>
                <button
                  type="button"
                  className="ghost-button"
                  onClick={() => setSharing(detail.page)}
                >
                  <Share2 size={14} />
                  Share
                </button>
                <button
                  type="button"
                  className="ghost-button"
                  onClick={() => void remove(detail.page)}
                >
                  <Trash2 size={14} />
                  Delete
                </button>
              </div>
            </div>

            {detail.page.status === "drifted" && (
              <p className="page-drift-banner">
                Some passages cited here have changed since this page was
                published. The page still shows what was published — that is
                the point — so re-publish the thread if you want the new text.
              </p>
            )}

            <article className="document-preview">
              <ReactMarkdown remarkPlugins={[remarkMath]} rehypePlugins={[rehypeKatex]}>
                {detail.body_md}
              </ReactMarkdown>
            </article>

            {detail.citations.length > 0 && (
              <section className="page-citations">
                <h2>Frozen citations</h2>
                <ol>
                  {detail.citations.map((citation) => (
                    <li key={citation.marker}>
                      <span className="page-citation-head">
                        <strong>[{citation.marker}]</strong>{" "}
                        {citation.filename || "unnamed source"}
                        <StatusPip status={citation.status} />
                      </span>
                      <blockquote title={citation.frozen_excerpt}>
                        {citation.frozen_excerpt || "No passage was pinned here."}
                      </blockquote>
                    </li>
                  ))}
                </ol>
              </section>
            )}
          </>
        )}
      </section>

      {sharing && (
        <ShareLinksModal
          kind="page"
          resourceId={sharing.id}
          resourceName={sharing.title}
          close={() => setSharing(null)}
        />
      )}
    </div>
  );
}
