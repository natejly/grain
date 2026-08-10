"use client";

import { Activity, Check, X } from "lucide-react";
import type { AuditEvent, ToolCall } from "@workspace/api-client";
import { formatRelative } from "./shared";

export type ActivityViewProps = {
  calls: ToolCall[];
  events: AuditEvent[];
  decide: (call: ToolCall, decision: "approved" | "denied") => Promise<void>;
  activeRun: string | null;
};

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
              <article className="approval-card" key={call.id}>
                <div className="approval-card-top">
                  <div className="tool-glyph">
                    <Activity size={17} />
                  </div>
                  <div>
                    <span>Assistant request</span>
                    <strong>{call.tool_name}</strong>
                  </div>
                </div>
                <div className="request-url">{call.request_url}</div>
                <div className="decision-buttons">
                  <button onClick={() => void decide(call, "denied")}>
                    <X size={15} />
                    Deny
                  </button>
                  <button
                    className="approve"
                    onClick={() => void decide(call, "approved")}
                  >
                    <Check size={15} />
                    Approve once
                  </button>
                </div>
              </article>
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
