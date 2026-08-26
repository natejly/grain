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
 * content — a share link is a window, not a snapshot.
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
            ? ` · ${resource.kind === "dashboard" ? "queried" : "updated"} ${new Date(
                stamp,
              ).toLocaleString()}`
            : ""}
        </span>
      </header>
      <div className="published-app-content">
        <section className="published-card">
          {resource.kind === "dashboard" ? (
            <Snapshot dashboard={asSnapshot(resource)} />
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
