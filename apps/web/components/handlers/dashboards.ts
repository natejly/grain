"use client";

import {
  ApiError,
  type Dashboard,
  type DashboardPin,
  type DashboardTemplate,
  type GeneratedApp,
} from "@workspace/api-client";
import type { Dispatch, RefObject, SetStateAction } from "react";
import { api } from "../api";
import type { DashboardResultState } from "../views/dashboard-grid";
import type { Tile } from "../views/dashboard-format";
import { describeError, slugify } from "../views/shared";

export type DashboardHandlerDeps = {
  setError: Dispatch<SetStateAction<string>>;
  setApps: Dispatch<SetStateAction<GeneratedApp[]>>;
  setDashboards: Dispatch<SetStateAction<Dashboard[]>>;
  setDashboardPins: Dispatch<SetStateAction<DashboardPin[]>>;
  setDashboardResults: Dispatch<SetStateAction<Record<string, DashboardResultState>>>;
  /** Dashboards already asked for, so a re-render is not a second query. */
  requested: RefObject<Set<string>>;
  refreshSecondary: () => Promise<void>;
};

export function createDashboardHandlers({
  setError,
  setApps,
  setDashboards,
  setDashboardPins,
  setDashboardResults,
  requested,
  refreshSecondary,
}: DashboardHandlerDeps) {
  /**
   * Creates the shell for a new dashboard. Slugs are unique workspace-wide, so
   * a collision retries with a short suffix rather than failing the first send.
   */
  async function createDashboard(
    name: string,
    visibility: "private" | "public",
  ): Promise<GeneratedApp> {
    setError("");
    const base = slugify(name) || "dashboard";
    for (let attempt = 0; attempt < 4; attempt += 1) {
      const slug =
        attempt === 0 ? base : `${base}-${Math.random().toString(36).slice(2, 6)}`;
      try {
        const created = await api.createApp({
          name,
          slug,
          visibility,
          app_type: "code",
          dashboard_ids: [],
        });
        setApps((items) => [created, ...items]);
        return created;
      } catch (caught) {
        if (caught instanceof ApiError && caught.status === 409) continue;
        throw caught;
      }
    }
    throw new Error("Could not find a free address for that name");
  }

  async function generateDashboard(
    app: GeneratedApp,
    prompt: string,
    datasetIds: string[],
  ): Promise<GeneratedApp> {
    const updated = await api.generateApp(app.id, {
      prompt,
      dataset_ids: datasetIds,
    });
    setApps((items) => items.map((item) => (item.id === updated.id ? updated : item)));
    await refreshSecondary();
    return updated;
  }

  async function publishGeneratedApp(app: GeneratedApp, releaseId: string) {
    setError("");
    try {
      const updated = await api.publishAppRelease(app.id, releaseId);
      setApps((items) => items.map((item) => (item.id === updated.id ? updated : item)));
      await refreshSecondary();
    } catch (caught) {
      setError(describeError(caught, "Could not publish this version"));
    }
  }

  async function rollbackGeneratedApp(app: GeneratedApp, releaseId: string) {
    setError("");
    try {
      const updated = await api.rollbackAppRelease(app.id, releaseId);
      setApps((items) => items.map((item) => (item.id === updated.id ? updated : item)));
      await refreshSecondary();
    } catch (caught) {
      setError(describeError(caught, "Could not roll back"));
    }
  }

  // ------------------------------------------------------------------------
  // The dashboards the *agent* writes: a dataset, a query and a shape. Nothing
  // below creates one — that is a tool call, made in chat — and that asymmetry
  // is the feature. What a person does here is pin, arrange, bind and delete.

  /**
   * Run a dashboard's query and hold the answer.
   *
   * Cached by dashboard id, because a tile is re-rendered by every drag and
   * every unrelated refetch, and a query per render is a query per frame.
   * `force` is the Refresh button, which is the only thing that re-asks.
   */
  function runDashboard(dashboardId: string, force = false) {
    // The guard is a ref rather than a look at `dashboardResults`, because the
    // caller is an effect that runs once per pin: reading state would let two
    // tiles mounted in the same commit both see "nothing yet" and both query.
    if (!force && requested.current.has(dashboardId)) return;
    requested.current.add(dashboardId);
    setDashboardResults((state) => ({
      ...state,
      [dashboardId]: { status: "loading" },
    }));
    void api
      .runDashboard(dashboardId)
      .then((run) =>
        setDashboardResults((state) => ({
          ...state,
          [dashboardId]: { status: "ready", result: run.result },
        })),
      )
      .catch((caught) =>
        setDashboardResults((state) => ({
          ...state,
          [dashboardId]: {
            status: "failed",
            // Named on the tile rather than in the global toast: a chart that
            // cannot run is a fact about that chart, and a banner over the
            // whole page does not say which of six tiles is broken.
            error: describeError(caught, "This dashboard could not run"),
          },
        })),
      );
  }

  async function pinDashboard(dashboardId: string) {
    setError("");
    try {
      const pin = await api.pinDashboard(dashboardId);
      setDashboardPins((items) => [
        ...items.filter((item) => item.dashboard.id !== dashboardId),
        pin,
      ]);
    } catch (caught) {
      setError(describeError(caught, "Could not pin that dashboard"));
    }
  }

  async function unpinDashboard(dashboardId: string) {
    setError("");
    try {
      await api.unpinDashboard(dashboardId);
      setDashboardPins((items) =>
        items.filter((item) => item.dashboard.id !== dashboardId),
      );
    } catch (caught) {
      setError(describeError(caught, "Could not unpin that dashboard"));
    }
  }

  /**
   * Save the arrangement. One write for the whole board, because moving a tile
   * moves its neighbours and a per-tile save leaves the grid inconsistent
   * between requests.
   */
  async function saveDashboardLayout(tiles: Tile[]) {
    try {
      setDashboardPins(await api.saveDashboardLayout(tiles));
    } catch (caught) {
      setError(describeError(caught, "Could not save that arrangement"));
    }
  }

  /**
   * Bind a template to a dataset. Refusals are re-thrown on purpose: the form
   * that asked shows them beside the fields, and flattening a list of missing
   * columns into the global toast is how "it didn't work" reaches a user with
   * nothing to do about it.
   */
  async function bindDashboardTemplate(
    template: DashboardTemplate,
    payload: {
      name: string;
      dataset_id: string;
      column_bindings: Record<string, string>;
    },
  ): Promise<Dashboard> {
    const dashboard = await api.bindDashboardTemplate(template.id, payload);
    setDashboards((items) => [dashboard, ...items]);
    return dashboard;
  }

  async function removeDashboard(dashboard: Dashboard) {
    if (!window.confirm(`Delete “${dashboard.name}”?`)) return;
    setError("");
    try {
      await api.deleteDashboard(dashboard.id);
      requested.current.delete(dashboard.id);
      setDashboards((items) => items.filter((item) => item.id !== dashboard.id));
      setDashboardPins((items) =>
        items.filter((item) => item.dashboard.id !== dashboard.id),
      );
    } catch (caught) {
      setError(describeError(caught, "Could not delete that dashboard"));
    }
  }

  return {
    createDashboard,
    generateDashboard,
    publishGeneratedApp,
    rollbackGeneratedApp,
    runDashboard,
    pinDashboard,
    unpinDashboard,
    saveDashboardLayout,
    bindDashboardTemplate,
    removeDashboard,
  };
}
