"use client";

import type { GeneratedApp } from "@workspace/api-client";
import { Plus } from "lucide-react";
import { DashboardTile } from "./dashboard-tile";

/**
 * The workspace's generated apps: programs the sandbox builds and publishes,
 * with releases, visibility and rollback of their own.
 *
 * They used to share a page with Dashboards, which made both things harder to
 * name — the page's "Add dashboard" button opened this editor and built an
 * app, and a user asking for a chart was handed a program. A dashboard is a
 * query and a shape the agent writes during a conversation; an app is a thing
 * you build here. Two tabs, two words, each meaning one thing.
 */
export type AppsViewProps = {
  apps: GeneratedApp[];
  openEditor: (value: string | "new") => void;
  publish: (app: GeneratedApp, releaseId: string) => Promise<void>;
  rollback: (app: GeneratedApp, releaseId: string) => Promise<void>;
};

export function AppsView({ apps, openEditor, publish, rollback }: AppsViewProps) {
  return (
    <section className="content-page dashboards-page">
      <div className="page-heading">
        <div>
          <h1>Apps</h1>
          <p>Programs the sandbox builds and publishes. They run in their own frame.</p>
        </div>
        <div className="page-heading-actions">
          <button className="primary-button" onClick={() => openEditor("new")}>
            <Plus size={16} />
            New app
          </button>
        </div>
      </div>

      <div className="dashboard-gallery">
        {apps.map((app) => (
          <DashboardTile
            key={app.id}
            app={app}
            open={() => openEditor(app.id)}
            publish={publish}
            rollback={rollback}
          />
        ))}
        {apps.length === 0 && (
          <button className="dashboard-add-tile" onClick={() => openEditor("new")}>
            <Plus size={22} />
            New app
          </button>
        )}
      </div>
    </section>
  );
}
