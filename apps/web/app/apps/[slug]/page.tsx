import type { PublishedApp } from "@workspace/api-client";
import { notFound } from "next/navigation";
import { PublishedCodeFrame } from "../../../components/published-code-frame";
import { Snapshot } from "../../../components/snapshot-renderer";

export const dynamic = "force-dynamic";

async function loadApp(slug: string): Promise<PublishedApp> {
  const apiUrl =
    process.env.API_INTERNAL_URL ||
    process.env.NEXT_PUBLIC_API_URL ||
    "http://localhost:8000";
  const response = await fetch(
    `${apiUrl}/published/apps/${encodeURIComponent(slug)}`,
    { cache: "no-store" },
  );
  if (response.status === 404) notFound();
  if (!response.ok) {
    throw new Error("Published app is temporarily unavailable");
  }
  return (await response.json()) as PublishedApp;
}

export default async function PublishedAppPage({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;
  const app = await loadApp(slug);
  return (
    <main className="published-app-shell">
      <header className="published-app-header">
        <h1>{app.name}</h1>
        {app.description && <p>{app.description}</p>}
        <span>
          Release v{app.version} · published{" "}
          {new Date(app.published_at).toLocaleDateString()}
        </span>
      </header>
      <div className="published-app-content">
        {app.manifest.kind === "code" ? (
          <PublishedCodeFrame
            slug={app.slug}
            name={app.name}
            snapshots={app.manifest.snapshots || {}}
          />
        ) : (
          (app.manifest.dashboards || []).map((dashboard) => (
            <section className="published-card" key={dashboard.id}>
              <header>
                <h2>{dashboard.name}</h2>
                {dashboard.description && <p>{dashboard.description}</p>}
              </header>
              <Snapshot dashboard={dashboard} />
            </section>
          ))
        )}
      </div>
    </main>
  );
}
