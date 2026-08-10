"use client";

import { Plus } from "lucide-react";
import type { GeneratedApp } from "@workspace/api-client";
import { DashboardTile } from "./dashboard-tile";

export type DashboardsViewProps = {
  apps: GeneratedApp[];
  openEditor: (value: string | "new") => void;
  publish: (app: GeneratedApp, releaseId: string) => Promise<void>;
  rollback: (app: GeneratedApp, releaseId: string) => Promise<void>;
};

export function DashboardsView({ apps, openEditor, publish, rollback }: DashboardsViewProps) {
  return (
    <section className="content-page dashboards-page">
      <div className="page-heading">
        <div>
          <h1>Dashboards</h1>
        </div>
        <button className="primary-button" onClick={() => openEditor("new")}>
          <Plus size={16} />
          Add dashboard
        </button>
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
        <button className="dashboard-add-tile" onClick={() => openEditor("new")}>
          <Plus size={22} />
          Add dashboard
        </button>
      </div>
    </section>
  );
}
