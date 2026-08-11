"use client";

import type {
  Dashboard,
  DashboardPin,
  DashboardTemplate,
  Dataset,
  GeneratedApp,
} from "@workspace/api-client";
import { Plus } from "lucide-react";
import { useEffect, useRef } from "react";
import { DashboardCatalog, DashboardTemplates } from "./dashboard-catalog";
import { DashboardGrid, type DashboardResultState } from "./dashboard-grid";
import type { Tile } from "./dashboard-format";
import { DashboardTile } from "./dashboard-tile";

/**
 * One page, two kinds of thing, and the order says which one the product is
 * about.
 *
 * At the top is *your* screen: the dashboards you pinned, arranged where you
 * put them. Nothing on it was made here — the agent writes dashboards during a
 * conversation and this page never offers to — which is the whole product
 * decision made visible. Underneath are templates waiting for data, and below
 * those the generated apps, which are programs rather than charts and keep
 * their own gallery.
 */

export type DashboardsViewProps = {
  apps: GeneratedApp[];
  openEditor: (value: string | "new") => void;
  publish: (app: GeneratedApp, releaseId: string) => Promise<void>;
  rollback: (app: GeneratedApp, releaseId: string) => Promise<void>;

  dashboards: Dashboard[];
  templates: DashboardTemplate[];
  datasets: Dataset[];
  pins: DashboardPin[];
  pinnedIds: Set<string>;
  results: Record<string, DashboardResultState>;
  runDashboard: (dashboardId: string, force?: boolean) => void;
  pinDashboard: (dashboardId: string) => Promise<void>;
  unpinDashboard: (dashboardId: string) => Promise<void>;
  saveDashboardLayout: (tiles: Tile[]) => Promise<void>;
  bindDashboardTemplate: (
    template: DashboardTemplate,
    payload: {
      name: string;
      dataset_id: string;
      column_bindings: Record<string, string>;
    },
  ) => Promise<Dashboard>;
  removeDashboard: (dashboard: Dashboard) => Promise<void>;
  /** Which pinned tile to reveal, set by the rail. Cleared once revealed. */
  focused: string | null;
  setFocused: (dashboardId: string | null) => void;
};

export function DashboardsView({
  apps,
  openEditor,
  publish,
  rollback,
  dashboards,
  templates,
  datasets,
  pins,
  pinnedIds,
  results,
  runDashboard,
  pinDashboard,
  unpinDashboard,
  saveDashboardLayout,
  bindDashboardTemplate,
  removeDashboard,
  focused,
  setFocused,
}: DashboardsViewProps) {
  const revealed = useRef<string | null>(null);

  useEffect(() => {
    if (!focused || revealed.current === focused) return;
    revealed.current = focused;
    // Runs after paint and does nothing if the tile is not there yet — a
    // dashboard focused from the rail the moment it was pinned arrives one
    // render later, and the outline alone is enough to find it then.
    document
      .getElementById(`pinned-dashboard-${focused}`)
      ?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    // The outline is a "here it is", not a selection: it clears itself so the
    // next visit to this page does not open with a tile mysteriously ringed.
    const timer = window.setTimeout(() => setFocused(null), 2500);
    return () => window.clearTimeout(timer);
  }, [focused, setFocused]);

  return (
    <section className="content-page dashboards-page">
      <div className="page-heading">
        <div>
          <h1>Dashboards</h1>
        </div>
        <div className="page-heading-actions">
          <DashboardCatalog
            dashboards={dashboards}
            pinnedIds={pinnedIds}
            pin={pinDashboard}
            unpin={unpinDashboard}
            remove={removeDashboard}
            // Launching *is* pinning: there is no single-dashboard page to
            // send someone to, and a chart you opened once is a chart you
            // wanted on your screen. Already pinned, it is merely revealed.
            open={(dashboardId) => {
              if (!pinnedIds.has(dashboardId)) void pinDashboard(dashboardId);
              setFocused(dashboardId);
            }}
          />
          <button className="primary-button" onClick={() => openEditor("new")}>
            <Plus size={16} />
            Add dashboard
          </button>
        </div>
      </div>

      <DashboardGrid
        pins={pins}
        results={results}
        run={runDashboard}
        unpin={unpinDashboard}
        saveLayout={saveDashboardLayout}
        focused={focused}
      />

      <DashboardTemplates
        templates={templates}
        datasets={datasets}
        bind={bindDashboardTemplate}
      />

      <section className="dashboard-apps" aria-label="Generated apps">
        <h2>Apps</h2>
        <p className="section-note">
          Programs the sandbox builds and publishes, rather than a query and a
          shape. They run in their own frame.
        </p>
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
    </section>
  );
}
