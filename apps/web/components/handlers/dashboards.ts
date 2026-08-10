"use client";

import {
  ApiError,
  type GeneratedApp,
} from "@workspace/api-client";
import type { Dispatch, SetStateAction } from "react";
import { api } from "../api";
import { describeError, slugify } from "../views/shared";

export type DashboardHandlerDeps = {
  setError: Dispatch<SetStateAction<string>>;
  setApps: Dispatch<SetStateAction<GeneratedApp[]>>;
  refreshSecondary: () => Promise<void>;
};

export function createDashboardHandlers({
  setError,
  setApps,
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

  return {
    createDashboard,
    generateDashboard,
    publishGeneratedApp,
    rollbackGeneratedApp,
  };
}
