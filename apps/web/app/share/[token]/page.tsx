import type {
  AppDashboardSnapshot,
  DashboardSpec,
  SharedResource,
} from "@workspace/api-client";
import { notFound } from "next/navigation";
import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkMath from "remark-math";
import { Snapshot } from "../../../components/snapshot-renderer";

export const dynamic = "force-dynamic";

/**
 * The public face of a share link — `app/apps/[slug]`'s pattern exactly: a
 * server component, no SessionProvider, one direct fetch of the public API
 * route (the token is the whole credential, so no cookies are needed or
 * sent), and a 404 for every way a link can not-work. A dashboard's numbers
 * are re-queried live by the server on every load; a document is its current
 * content — a share link is a window, not a snapshot. A conversation is the
 * same live window a document is: the transcript as it stands at request
 * time, never a frozen copy.
 *
 * A PAGE IS THE DELIBERATE EXCEPTION, and the reason this paragraph exists.
 * A page is a snapshot on purpose: its whole value is that a reader following
 * a marker sees the passage the answer was written from, so the body and every
 * excerpt below are what the publisher published, not what the workspace holds
 * now. Staleness is REPORTED (`page_drifted`), never papered over by quietly
 * serving newer text. Anything that "fixed the inconsistency" by making pages
 * live would destroy the feature.
 */
async function loadShared(token: string): Promise<SharedResource> {
  const apiUrl =
    process.env.API_INTERNAL_URL ||
    process.env.NEXT_PUBLIC_API_URL ||
    "http://localhost:8000";
  const response = await fetch(
    `${apiUrl}/shared/${encodeURIComponent(token)}`,
    { cache: "no-store" },
  );
  if (response.status === 404) notFound();
  if (!response.ok) {
    throw new Error("This shared page is temporarily unavailable");
  }
  return (await response.json()) as SharedResource;
}

/**
 * Render an API timestamp as explicit UTC — what the Markdown export does.
 *
 * The API serializes naive-UTC datetimes; `new Date(naive).toLocaleString()`
 * would parse them as LOCAL time (wrong by the viewer's whole offset), and
 * this is a server component besides, so "local" would be the server's zone,
 * not the reader's. A labelled UTC instant is the honest, deterministic
 * rendering. Tolerates an offset-suffixed stamp too, in case the API grows
 * one.
 */
function formatUtc(stamp: string): string {
  const explicit = /(?:Z|[+-]\d{2}:?\d{2})$/.test(stamp) ? stamp : `${stamp}Z`;
  const date = new Date(explicit);
  if (Number.isNaN(date.getTime())) return stamp;
  return `${date.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

/** The stored spec says how to draw; the live result says what. Adapt both to
 * the snapshot renderer the published-app page already uses. */
function asSnapshot(resource: SharedResource): AppDashboardSnapshot {
  let visualization: DashboardSpec["visualization"] = "table";
  let xField: string | null = null;
  let yFields: string[] = [];
  try {
    const spec = JSON.parse(resource.spec_json) as Partial<DashboardSpec>;
    visualization = spec.visualization ?? "table";
    xField = spec.x_field ?? null;
    yFields = spec.y_fields ?? [];
  } catch {
    // An unreadable spec still has an answer to show — draw it as a table.
  }
  return {
    id: "shared",
    name: resource.title,
    description: "",
    visualization,
    x_field: xField,
    y_fields: yFields,
    result: {
      columns: resource.columns,
      rows: resource.rows,
      row_count: resource.rows.length,
      truncated: false,
      elapsed_ms: 0,
    },
  };
}

export default async function SharedResourcePage({
  params,
}: {
  params: Promise<{ token: string }>;
}) {
  const { token } = await params;
  const resource = await loadShared(token);
  const stamp =
    resource.kind === "dashboard" ? resource.generated_at : resource.updated_at;
  return (
    <main className="published-app-shell">
      <header className="published-app-header">
        <h1>{resource.title}</h1>
        <span>
          Shared read-only
          {stamp
            ? ` · ${resource.kind === "dashboard" ? "queried" : "updated"} ${formatUtc(stamp)}`
            : ""}
        </span>
      </header>
      <div className="published-app-content">
        <section className="published-card">
          {resource.kind === "dashboard" ? (
            <Snapshot dashboard={asSnapshot(resource)} />
          ) : resource.kind === "conversation" ? (
            <div className="shared-transcript">
              {/* A capped payload says so — a tail passed off as the whole
                  thread would misread as the conversation's beginning. */}
              {resource.truncated && (
                <p className="shared-truncated">
                  Earlier messages in this conversation are not shown.
                </p>
              )}
              {resource.messages.map((message, index) => (
                <article key={index} className={`shared-turn ${message.role}`}>
                  <header>
                    <strong>
                      {/* An aside is labelled, exactly as the Markdown export
                          labels it: unmarked, a "/btw" context note reads as
                          a prompt the assistant then appears to ignore. */}
                      {message.role === "assistant"
                        ? "Assistant"
                        : `${message.sender_name || "User"}${
                            message.is_aside ? " (aside)" : ""
                          }`}
                    </strong>
                    <time>{formatUtc(message.created_at)}</time>
                  </header>
                  {message.role === "assistant" ? (
                    <ReactMarkdown
                      remarkPlugins={[remarkMath]}
                      rehypePlugins={[rehypeKatex]}
                    >
                      {message.content}
                    </ReactMarkdown>
                  ) : (
                    <p>{message.content}</p>
                  )}
                </article>
              ))}
            </div>
          ) : resource.kind === "page" ? (
            <div className="shared-page">
              {resource.page_drifted && (
                <p className="shared-truncated">
                  Some passages cited here have changed since this page was
                  published.
                </p>
              )}
              <div className="document-preview">
                <ReactMarkdown
                  remarkPlugins={[remarkMath]}
                  rehypePlugins={[rehypeKatex]}
                >
                  {resource.page_body ?? ""}
                </ReactMarkdown>
              </div>
              {(resource.page_citations ?? []).length > 0 && (
                <section className="shared-page-citations">
                  <h2>Citations</h2>
                  {/* Printed in full as well as carried on `title`: a server
                      component cannot own popover state, so the hover is the
                      native tooltip and the excerpt is also on the page —
                      which is what keeps it readable on a phone. */}
                  <ol>
                    {(resource.page_citations ?? []).map((citation) => (
                      <li key={citation.marker} title={citation.frozen_excerpt}>
                        <strong>[{citation.marker}]</strong>{" "}
                        {citation.filename || "unnamed source"}
                        {citation.status !== "frozen" && (
                          <em> — {citation.status} since publication</em>
                        )}
                        <blockquote>{citation.frozen_excerpt}</blockquote>
                      </li>
                    ))}
                  </ol>
                </section>
              )}
            </div>
          ) : resource.document_kind === "text" ? (
            <pre className="document-plain">{resource.content}</pre>
          ) : (
            <div className="document-preview">
              <ReactMarkdown
                remarkPlugins={[remarkMath]}
                rehypePlugins={[rehypeKatex]}
              >
                {resource.content}
              </ReactMarkdown>
            </div>
          )}
        </section>
      </div>
    </main>
  );
}
