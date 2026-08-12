"use client";

import { Plug, RefreshCw } from "lucide-react";
import type { IntegrationProvider } from "@workspace/api-client";
import { FormEvent, useState } from "react";
import { describeError, formatRelative } from "./shared";

const PROVIDER_LABELS: Record<string, string> = {
  google: "Google (Gmail)",
  strava: "Strava",
  garmin: "Garmin Connect",
};

export type IntegrationsViewProps = {
  integrations: IntegrationProvider[];
  connect: (provider: string) => Promise<void>;
  connectGarmin: (email: string, password: string) => Promise<void>;
  disconnect: (accountId: string) => Promise<void>;
  sync: (accountId: string) => Promise<void>;
  setError: (message: string) => void;
};

export function IntegrationsView({
  integrations,
  connect,
  connectGarmin,
  disconnect,
  sync,
  setError,
}: IntegrationsViewProps) {
  const [syncing, setSyncing] = useState<string | null>(null);
  const [garminEmail, setGarminEmail] = useState("");
  const [garminPassword, setGarminPassword] = useState("");
  const [garminWorking, setGarminWorking] = useState(false);

  async function submitGarmin(event: FormEvent) {
    event.preventDefault();
    if (!garminEmail.trim() || !garminPassword) return;
    setGarminWorking(true);
    try {
      await connectGarmin(garminEmail.trim(), garminPassword);
      setGarminEmail("");
      setGarminPassword("");
    } catch (caught) {
      setError(describeError(caught, "Garmin login failed"));
    } finally {
      setGarminWorking(false);
    }
  }

  async function runSync(accountId: string) {
    setSyncing(accountId);
    try {
      await sync(accountId);
    } finally {
      setSyncing(null);
    }
  }

  return (
    <section className="content-page integrations-page">
      <div className="page-heading">
        <div>
          <h1>Integrations</h1>
        </div>
      </div>

      <div className="integration-grid">
        {integrations.map((item) => {
          const name = PROVIDER_LABELS[item.provider] || item.provider;
          return (
            <article className="integration-card" key={item.provider}>
              <div className="integration-card-head">
                <div className="integration-icon">
                  <Plug size={18} />
                </div>
                <div>
                  <strong>{name}</strong>
                  <span>
                    {item.account
                      ? `${item.account.external_account || "connected"} · ${item.account.status}`
                      : item.configured
                        ? "Not connected"
                        : "Not configured"}
                  </span>
                </div>
              </div>
              {!item.configured && !item.account && (
                <small>
                  Set the client credentials and INTEGRATIONS_ENCRYPTION_KEY in{" "}
                  <code>.env</code> to enable this provider.
                </small>
              )}
              <div className="integration-actions">
                {item.account ? (
                  <>
                    <button
                      className="primary-button"
                      onClick={() => void runSync(item.account!.id)}
                      disabled={syncing === item.account.id}
                    >
                      <RefreshCw
                        size={14}
                        className={syncing === item.account.id ? "spin" : ""}
                      />
                      {syncing === item.account.id ? "Syncing…" : "Sync now"}
                    </button>
                    <button onClick={() => void disconnect(item.account!.id)}>
                      Disconnect
                    </button>
                    {item.account.last_sync_at && (
                      <span>synced {formatRelative(item.account.last_sync_at)}</span>
                    )}
                  </>
                ) : item.provider === "garmin" ? (
                  <form className="garmin-form" onSubmit={(event) => void submitGarmin(event)}>
                    <input
                      type="email"
                      value={garminEmail}
                      onChange={(event) => setGarminEmail(event.target.value)}
                      aria-label="Garmin email"
                      autoComplete="off"
                      disabled={!item.configured}
                    />
                    <input
                      type="password"
                      value={garminPassword}
                      onChange={(event) => setGarminPassword(event.target.value)}
                      aria-label="Garmin password"
                      autoComplete="off"
                      disabled={!item.configured}
                    />
                    <button
                      className="primary-button"
                      type="submit"
                      disabled={
                        !item.configured ||
                        !garminEmail.trim() ||
                        !garminPassword ||
                        garminWorking
                      }
                    >
                      {garminWorking ? "Logging in…" : "Connect"}
                    </button>
                  </form>
                ) : (
                  <button
                    className="primary-button"
                    onClick={() => void connect(item.provider)}
                    disabled={!item.configured}
                  >
                    Connect
                  </button>
                )}
              </div>
            </article>
          );
        })}
      </div>

      <div className="integration-note">
        Garmin uses the unofficial Connect API, so it can break when Garmin changes
        things, and MFA-protected accounts are not supported yet. Garmin devices that
        auto-sync to Strava are covered by the Strava integration instead.
      </div>
    </section>
  );
}
