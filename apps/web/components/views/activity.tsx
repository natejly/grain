"use client";

import { Activity, Check, X } from "lucide-react";
import { useState } from "react";
import type { AgentToolCall, AuditEvent } from "@workspace/api-client";
import type { ToolDecision } from "./chat";
import { ProposalDiff } from "./document-pending";
import { formatRelative } from "./shared";

/**
 * Approvals here are `AgentToolCall`s — the same records the chat cards and the
 * Workflows view decide, read through the same `decideAgentCall`.
 *
 * The panel used to read the legacy `ToolCall`/`Tool` tables instead. Nothing
 * creates a `Tool` row outside `seed_dev_workspace`, so outside a dev database
 * this queue could never fill: it promised a review surface and always showed
 * "no pending requests", however many runs were actually parked waiting.
 */
export type ActivityViewProps = {
  calls: AgentToolCall[];
  events: AuditEvent[];
  decide: ToolDecision;
  activeRun: string | null;
};

/**
 * One parked call. `remember` is offered here for the same reason chat offers
 * it: the answer to "may the agent do this" is usually about the tool, not this
 * one call, and without it the queue re-fills with the same request.
 */
function ApprovalRequest({
  call,
  decide,
}: {
  call: AgentToolCall;
  decide: ToolDecision;
}) {
  const [remember, setRemember] = useState(false);
  const [busy, setBusy] = useState(false);

  async function choose(decision: "approved" | "denied") {
    setBusy(true);
    try {
      await decide(call, decision, remember);
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className="approval-card">
      <div className="approval-card-top">
        <div className="tool-glyph">
          <Activity size={17} />
        </div>
        <div>
          <span>Assistant request</span>
          <strong>{call.name}</strong>
        </div>
      </div>
      {call.proposal_preview && (
        <div className="approval-proposal">
          <ProposalDiff preview={call.proposal_preview} />
        </div>
      )}
      <label className="approval-remember">
        <input
          type="checkbox"
          checked={remember}
          onChange={(event) => setRemember(event.target.checked)}
        />
        Always allow {call.name}
      </label>
      <div className="decision-buttons">
        <button disabled={busy} onClick={() => void choose("denied")}>
          <X size={15} />
          Deny
        </button>
        <button
          className="approve"
          disabled={busy}
          onClick={() => void choose("approved")}
        >
          <Check size={15} />
          {remember ? "Approve" : "Approve once"}
        </button>
      </div>
    </article>
  );
}

export function ActivityView({ calls, events, decide, activeRun }: ActivityViewProps) {
  const pending = calls.filter((call) => call.status === "proposed");
  return (
    <section className="content-page activity-page">
      <div className="page-heading">
        <div>
          <h1>Activity</h1>
          <p>Review tool requests and recorded actions.</p>
        </div>
      </div>

      <div className="activity-grid">
        <div className="approval-panel">
          <div className="panel-title">
            <div>
              <strong>Pending approvals</strong>
            </div>
            <span className="panel-count">{pending.length}</span>
          </div>
          {pending.length === 0 ? (
            <div className="approval-empty">
              <div>
                <Check size={18} />
              </div>
              <strong>No pending requests</strong>
              <p>Tool calls requiring approval appear here.</p>
            </div>
          ) : (
            pending.map((call) => (
              <ApprovalRequest key={call.id} call={call} decide={decide} />
            ))
          )}
        </div>

        <div className="audit-panel">
          <div className="panel-title">
            <div>
              <strong>Audit log</strong>
            </div>
            {activeRun && <span className="live-pill">Live</span>}
          </div>
          <div className="timeline">
            {events.length === 0 ? (
              <p className="timeline-empty">Activity will appear as you use the workspace.</p>
            ) : (
              events.map((event) => (
                <div className="timeline-event" key={event.id}>
                  <div className="timeline-dot" />
                  <div>
                    <strong>{event.action.replaceAll(".", " ")}</strong>
                    <span>
                      {event.resource_type} · {formatRelative(event.created_at)}
                    </span>
                    {Object.keys(event.detail).length > 0 && (
                      <small>
                        {Object.entries(event.detail)
                          .slice(0, 2)
                          .map(([key, value]) => `${key}: ${String(value)}`)
                          .join(" · ")}
                      </small>
                    )}
                  </div>
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
