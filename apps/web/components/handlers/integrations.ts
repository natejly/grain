"use client";

import type { IntegrationProvider } from "@workspace/api-client";
import type { Dispatch, SetStateAction } from "react";
import { api } from "../api";
import { describeError } from "../views/shared";

export type IntegrationHandlerDeps = {
  setError: Dispatch<SetStateAction<string>>;
  setIntegrations: Dispatch<SetStateAction<IntegrationProvider[]>>;
  refreshSecondary: () => Promise<void>;
  refreshExpansion: () => Promise<void>;
};

export function createIntegrationHandlers({
  setError,
  setIntegrations,
  refreshSecondary,
  refreshExpansion,
}: IntegrationHandlerDeps) {
  async function connectIntegration(provider: string) {
    setError("");
    try {
      const { authorize_url } = await api.connectIntegration(provider);
      window.location.href = authorize_url;
    } catch (caught) {
      setError(describeError(caught, "Could not start OAuth"));
    }
  }

  async function connectGarminAccount(email: string, password: string) {
    setError("");
    await api.connectGarmin(email, password);
    setIntegrations(await api.listIntegrations());
  }

  async function disconnectIntegration(accountId: string) {
    if (!window.confirm("Disconnect this account and remove its stored tokens?")) return;
    setError("");
    try {
      await api.disconnectIntegration(accountId);
      setIntegrations(await api.listIntegrations());
    } catch (caught) {
      setError(describeError(caught, "Could not disconnect"));
    }
  }

  async function syncIntegration(accountId: string) {
    setError("");
    try {
      await api.syncIntegration(accountId);
      for (let attempt = 0; attempt < 30; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
        const jobs = await api.listSyncJobs(accountId);
        if (jobs[0] && !["queued", "running"].includes(jobs[0].status)) {
          if (jobs[0].status === "failed") {
            setError(jobs[0].error || "Sync failed");
          }
          break;
        }
      }
      await refreshSecondary();
      await refreshExpansion();
    } catch (caught) {
      setError(describeError(caught, "Could not sync"));
    }
  }

  return {
    connectIntegration,
    connectGarminAccount,
    disconnectIntegration,
    syncIntegration,
  };
}
