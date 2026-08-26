"use client";

import type { DashboardSubscription } from "@workspace/api-client";
import { Mail, MailX, X } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "./api";
import {
  SUBSCRIPTION_PRESETS,
  describeSubscriptionSchedule,
  subscriptionsFor,
} from "./views/subscription-format";
import { describeError, formatRelative } from "./views/shared";

/**
 * The subscribe popover for one dashboard: pick a schedule, get the dashboard
 * mailed to you, see the mails already standing, and stop any of them.
 *
 * Two presets and no cron field, deliberately — this sits on a dashboard row,
 * and the person here wants their morning number, not an automations editor
 * (the Schedules view exists for the exotic case). The timezone shown is the
 * browser's, because "9:00" is meaningless until it says whose 9:00.
 *
 * Imports `api` directly, as the share-links modal does: self-contained, no
 * shell state touched, and it reuses that modal's CSS classes wholesale —
 * same shape of surface, same clothes.
 */
export function SubscribeModal({
  dashboardId,
  dashboardName,
  close,
}: {
  dashboardId: string;
  dashboardName: string;
  close: () => void;
}) {
  const [subscriptions, setSubscriptions] = useState<DashboardSubscription[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [problem, setProblem] = useState("");
  const [busy, setBusy] = useState("");

  const timezone =
    Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";

  useEffect(() => {
    let cancelled = false;
    api
      .listDashboardSubscriptions()
      .then((rows) => {
        if (cancelled) return;
        setSubscriptions(subscriptionsFor(rows, dashboardId));
        setLoaded(true);
      })
      .catch((caught) => {
        if (cancelled) return;
        setProblem(
          describeError(caught, "Could not load the existing subscriptions"),
        );
        setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [dashboardId]);

  async function subscribe(cron: string, presetId: string) {
    setProblem("");
    setBusy(presetId);
    try {
      const created = await api.createDashboardSubscription({
        dashboard_id: dashboardId,
        schedule_cron: cron,
        schedule_timezone: timezone,
      });
      setSubscriptions((rows) => [created, ...rows]);
    } catch (caught) {
      setProblem(describeError(caught, "Could not create that subscription"));
    } finally {
      setBusy("");
    }
  }

  async function unsubscribe(subscription: DashboardSubscription) {
    setProblem("");
    setBusy(subscription.id);
    try {
      await api.deleteDashboardSubscription(subscription.id);
      setSubscriptions((rows) =>
        rows.filter((row) => row.id !== subscription.id),
      );
    } catch (caught) {
      setProblem(describeError(caught, "Could not stop that subscription"));
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="share-modal-backdrop" onClick={close}>
      <section
        className="share-modal"
        role="dialog"
        aria-label={`Email ${dashboardName} on a schedule`}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="share-modal-header">
          <div>
            <strong>Email “{dashboardName}”</strong>
            <p className="field-hint">
              A snapshot of this dashboard, mailed to you on a schedule. Times
              are {timezone}.
            </p>
          </div>
          <button
            type="button"
            className="icon-button"
            aria-label="Close subscribe dialog"
            title="Close"
            onClick={close}
          >
            <X size={15} />
          </button>
        </header>

        {problem && (
          <p className="budget-problem" role="alert">
            {problem}
          </p>
        )}

        {SUBSCRIPTION_PRESETS.map((preset) => (
          <button
            key={preset.id}
            type="button"
            className="primary-button"
            disabled={busy === preset.id}
            onClick={() => void subscribe(preset.cron, preset.id)}
          >
            <Mail size={13} />
            {busy === preset.id ? "Subscribing…" : preset.label}
          </button>
        ))}

        {loaded && subscriptions.length === 0 ? (
          <p className="section-note">
            No subscriptions yet — nobody is mailed this dashboard.
          </p>
        ) : (
          <ul className="share-link-list">
            {subscriptions.map((subscription) => (
              <li key={subscription.id}>
                <div>
                  <span className="admin-tag">
                    {subscription.enabled ? "scheduled" : "off"}
                  </span>
                  <span className="share-link-meta">
                    {describeSubscriptionSchedule(
                      subscription.schedule_cron,
                      subscription.schedule_timezone,
                    )}
                    {" · "}
                    {subscription.last_dispatched_at
                      ? `last sent ${formatRelative(subscription.last_dispatched_at)}`
                      : "not sent yet"}
                  </span>
                </div>
                <button
                  type="button"
                  className="ghost-button"
                  disabled={busy === subscription.id}
                  onClick={() => void unsubscribe(subscription)}
                >
                  <MailX size={12} />
                  {busy === subscription.id ? "Stopping…" : "Unsubscribe"}
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
