"use client";

import {
  AtSign,
  Check,
  CircleDollarSign,
  Clock,
  ExternalLink,
  Inbox as InboxIcon,
  RefreshCw,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type {
  AgentToolCall,
  AuditEvent,
  InboxApproval,
  InboxFeed,
  InboxMention,
  WorkspaceMember,
} from "@workspace/api-client";
import { assigneeName, partitionApprovals } from "./approval-format";
import type { ToolDecision } from "./chat";
import { ProposalDiff } from "./proposal-diff";
import { formatRelative } from "./shared";
import { BUDGET_RUN_LABEL } from "./workflow-format";

/**
 * The Inbox: everything waiting on a person, from every origin, in one list.
 *
 * Reads `GET /api/inbox` — the server-side union with no scan bound — rather
 * than the shell's `agentCalls`, which is a fifty-row window of calls of any
 * status and is exactly how a parked approval used to vanish from every
 * surface. The shell's list still powers the chat transcript; this page is the
 * queue, and the queue must not have a horizon.
 *
 * Decisions go through the same `decideAgentCall` the chat cards use, so
 * approving here resumes the run through the same endpoint whatever parked it
 * (chat turn, workflow node, schedule). Budget holds render as their own
 * section with no approve/deny — there is no proposed call behind one, and
 * pretending otherwise would be a button that 409s. History is the audit
 * trail, unchanged from the old Activity page.
 */
export type InboxViewProps = {
  /**
   * The attention feed, fetched by the shell beside its other workspace lists
   * so the rail badge, the sidebar strip and this page cannot disagree about
   * what is waiting. Null until the first read lands.
   */
  feed: InboxFeed | null;
  /** Re-read the feed — after a decision, or on the Refresh button. */
  refreshFeed: () => void;
  events: AuditEvent[];
  decide: ToolDecision;
  activeRun: string | null;
  /** Jump to the thread a parked item came from. */
  openConversation: (conversationId: string) => void;
  /** Flip a mention out of the waiting set; the caller re-reads the feed. */
  resolveMention: (notificationId: string) => Promise<void>;
  /** Jump to whatever a mention deep-links: its thread, document or dashboard. */
  openMention: (mention: InboxMention) => void;
  /** The signed-in member's user id — what splits the queue into "assigned to
   * you" vs the rest. "" until bootstrap's first read lands. */
  identityId: string;
  /** The workspace member list for the assignee control (the same list the
   * @-picker reads; every member may see it). */
  loadMembers: () => Promise<WorkspaceMember[]>;
  /** Route one approval to a member, or back to anyone with "". */
  assignApproval: (callId: string, userId: string) => Promise<boolean>;
};

type Section = "approvals" | "holds" | "mentions" | "runs" | "history";

const ORIGIN_LABELS: Record<string, string> = {
  chat: "Chat",
  subject: "Document thread",
  workflow: "Workflow",
  schedule: "Schedule",
};

/**
 * The one-sentence headline above the machine name: who wants what, in words
 * a person triaging a queue can act on without expanding anything.
 */
function headline(row: InboxApproval): string {
  const actor =
    row.origin === "workflow"
      ? row.workflow_name
        ? `The “${row.workflow_name}” workflow`
        : "A workflow"
      : row.origin === "schedule"
        ? "A schedule"
        : "The agent";
  return `${actor} wants to run ${row.name}`;
}

/** decideAgentCall wants the shell's row shape; the feed row carries the two
 * fields the handler actually reads (id for the POST, run_id/status for the
 * optimistic update), so the rest is honest emptiness rather than a refetch. */
export function asCall(row: InboxApproval): AgentToolCall {
  return {
    id: row.id,
    run_id: row.run_id,
    conversation_id: row.conversation_id,
    name: row.name,
    arguments_json: "{}",
    status: "proposed",
    result_preview: "",
    error: "",
    latency_ms: 0,
    proposal_preview: row.proposal_preview,
    approved_by_mode: "",
    assigned_to: row.assigned_to,
    artifacts: [],
    created_at: row.created_at,
  };
}

function ApprovalRow({
  row,
  decide,
  focused,
  onOpen,
  onDecided,
  selfId,
  members,
  assign,
  dimmed,
}: {
  row: InboxApproval;
  decide: ToolDecision;
  focused: boolean;
  onOpen?: () => void;
  onDecided: () => void;
  selfId: string;
  members: WorkspaceMember[];
  assign: (callId: string, userId: string) => Promise<boolean>;
  /** True in the "assigned to others" group — their wait, not this reader's. */
  dimmed?: boolean;
}) {
  const [remember, setRemember] = useState(false);
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (focused) ref.current?.scrollIntoView({ block: "nearest" });
  }, [focused]);

  async function choose(decision: "approved" | "denied") {
    setBusy(true);
    try {
      await decide(asCall(row), decision, remember);
      onDecided();
    } finally {
      setBusy(false);
    }
  }

  async function route(userId: string) {
    setBusy(true);
    try {
      await assign(row.id, userId);
    } finally {
      setBusy(false);
    }
  }

  const classNames = [
    "approval-card",
    focused ? "focused" : "",
    dimmed ? "assigned-away" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <article ref={ref} className={classNames} data-call-id={row.id}>
      <div className="approval-card-top">
        <div className="tool-glyph">
          <InboxIcon size={17} />
        </div>
        <div>
          <span>
            {ORIGIN_LABELS[row.origin] ?? "Chat"}
            {row.conversation_title ? ` · ${row.conversation_title}` : ""}
            {" · waiting "}
            {formatRelative(row.created_at).replace(" ago", "")}
          </span>
          <strong>{headline(row)}</strong>
        </div>
        {onOpen && (
          <button
            type="button"
            className="ghost-button approval-open"
            onClick={onOpen}
          >
            <ExternalLink size={13} />
            Open thread
          </button>
        )}
      </div>
      {row.proposal_preview && (
        <div className="approval-proposal">
          <ProposalDiff preview={row.proposal_preview} />
        </div>
      )}
      <label className="approval-remember">
        <input
          type="checkbox"
          checked={remember}
          onChange={(event) => setRemember(event.target.checked)}
        />
        Always allow {row.name} in this workspace
      </label>
      <label className="approval-assignee">
        Waiting on
        <select
          value={row.assigned_to}
          disabled={busy}
          aria-label={`Assign ${row.name} to a member`}
          onChange={(event) => void route(event.target.value)}
        >
          <option value="">Anyone</option>
          {members.map((member) => (
            <option key={member.user_id} value={member.user_id}>
              {member.user_id === selfId ? `${member.name} (you)` : member.name}
            </option>
          ))}
          {/* A row routed to someone the list no longer names (a departed
              member) still has to render its truth rather than "Anyone". */}
          {row.assigned_to &&
            !members.some((member) => member.user_id === row.assigned_to) && (
              <option value={row.assigned_to}>
                {assigneeName(row.assigned_to, members)}
              </option>
            )}
        </select>
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

export function InboxView({
  feed,
  refreshFeed,
  events,
  decide,
  activeRun,
  openConversation,
  resolveMention,
  openMention,
  identityId,
  loadMembers,
  assignApproval,
}: InboxViewProps) {
  const [section, setSection] = useState<Section>("approvals");
  const [focusIndex, setFocusIndex] = useState(0);
  const [members, setMembers] = useState<WorkspaceMember[]>([]);

  // The assignee control needs names once, not per keystroke; the same
  // member-visible list the @-picker reads.
  useEffect(() => {
    let cancelled = false;
    void loadMembers().then((rows) => {
      if (!cancelled) setMembers(rows);
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Triage order: yours first, then anyone's, then — de-emphasized, at the
  // bottom — the rows routed to colleagues. The flattened order is what J/K
  // walk, so the keyboard and the page cannot disagree about "next". Memoised
  // because the flattened array is a dependency of the keyboard effect.
  const feedApprovals = feed?.approvals;
  const buckets = useMemo(
    () => partitionApprovals(feedApprovals ?? [], identityId),
    [feedApprovals, identityId],
  );
  const approvals = useMemo(
    () => [...buckets.mine, ...buckets.unassigned, ...buckets.others],
    [buckets],
  );
  const holds = feed?.budget_holds ?? [];
  const mentions = feed?.mentions ?? [];
  const runs = feed?.recent_runs ?? [];

  // J/K walk the queue, A/D decide the focused row — triage without a mouse.
  // Suppressed while anything focusable owns the keyboard, so typing a memo
  // into some other control cannot deny a write.
  useEffect(() => {
    if (section !== "approvals") return;
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target && /^(input|textarea|select)$/i.test(target.tagName)) return;
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.key === "j" || event.key === "J") {
        setFocusIndex((index) => Math.min(index + 1, Math.max(approvals.length - 1, 0)));
      } else if (event.key === "k" || event.key === "K") {
        setFocusIndex((index) => Math.max(index - 1, 0));
      } else if (event.key === "a" || event.key === "A" || event.key === "d" || event.key === "D") {
        const row = approvals[focusIndex];
        if (!row) return;
        const decision = event.key.toLowerCase() === "a" ? "approved" : "denied";
        void decide(asCall(row), decision, false).then(refreshFeed);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [section, approvals, focusIndex, decide, refreshFeed]);

  useEffect(() => {
    setFocusIndex((index) => Math.min(index, Math.max(approvals.length - 1, 0)));
  }, [approvals.length]);

  const tabs: Array<{ id: Section; label: string; count?: number }> = [
    { id: "approvals", label: "Needs approval", count: approvals.length },
    { id: "holds", label: "Budget holds", count: holds.length },
    { id: "mentions", label: "Mentions", count: mentions.length },
    { id: "runs", label: "Runs" },
    { id: "history", label: "History" },
  ];

  return (
    <section className="content-page activity-page">
      <div className="page-heading">
        <div>
          <h1>Inbox</h1>
          <p>Requests waiting on you, from every origin — nothing scrolls off.</p>
        </div>
        <div className="page-heading-actions">
          <button className="ghost-button" onClick={refreshFeed}>
            <RefreshCw size={14} />
            Refresh
          </button>
        </div>
      </div>

      <nav className="inbox-tabs" aria-label="Inbox sections">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            className={section === tab.id ? "inbox-tab active" : "inbox-tab"}
            aria-current={section === tab.id ? "page" : undefined}
            onClick={() => setSection(tab.id)}
          >
            {tab.label}
            {tab.count !== undefined && tab.count > 0 && (
              <span className="approval-count">{tab.count}</span>
            )}
          </button>
        ))}
      </nav>

      {section === "approvals" && (
        <div className="approval-panel inbox-queue">
          {feed === null ? (
            <p className="timeline-empty">Loading…</p>
          ) : approvals.length === 0 ? (
            <div className="approval-empty">
              <div>
                <Check size={18} />
              </div>
              <strong>Nothing needs you</strong>
              <p>Tool calls waiting for approval appear here, whatever started them.</p>
            </div>
          ) : (
            <>
              <p className="inbox-hint">
                Oldest first. <kbd>J</kbd>/<kbd>K</kbd> to move, <kbd>A</kbd> approve,{" "}
                <kbd>D</kbd> deny.
              </p>
              {(
                [
                  { label: "Assigned to you", rows: buckets.mine, offset: 0 },
                  {
                    label: "Unassigned",
                    rows: buckets.unassigned,
                    offset: buckets.mine.length,
                  },
                  {
                    label: "Assigned to others",
                    rows: buckets.others,
                    offset: buckets.mine.length + buckets.unassigned.length,
                  },
                ] as const
              ).map((group) => {
                if (group.rows.length === 0) return null;
                // One flat queue while nothing is routed; headers only earn
                // their space once assignment splits the list.
                const showHeader =
                  buckets.mine.length + buckets.others.length > 0;
                return (
                  <div key={group.label} className="approval-group">
                    {showHeader && (
                      <h2
                        className={
                          group.label === "Assigned to others"
                            ? "approval-group-title assigned-away"
                            : "approval-group-title"
                        }
                      >
                        {group.label}
                        <span className="approval-count">{group.rows.length}</span>
                      </h2>
                    )}
                    {group.rows.map((row, index) => (
                      <ApprovalRow
                        key={row.id}
                        row={row}
                        decide={decide}
                        focused={group.offset + index === focusIndex}
                        onOpen={
                          row.conversation_id
                            ? () => openConversation(row.conversation_id)
                            : undefined
                        }
                        onDecided={refreshFeed}
                        selfId={identityId}
                        members={members}
                        assign={assignApproval}
                        dimmed={group.label === "Assigned to others"}
                      />
                    ))}
                  </div>
                );
              })}
            </>
          )}
        </div>
      )}

      {section === "holds" && (
        <div className="approval-panel inbox-queue">
          {holds.length === 0 ? (
            <div className="approval-empty">
              <div>
                <Check size={18} />
              </div>
              <strong>Nothing held by the ceiling</strong>
              <p>Runs the spend limit parks appear here until an owner raises it.</p>
            </div>
          ) : (
            holds.map((hold) => (
              <article key={hold.run_id} className="approval-card">
                <div className="approval-card-top">
                  <div className="tool-glyph">
                    <CircleDollarSign size={17} />
                  </div>
                  <div>
                    <span>
                      {ORIGIN_LABELS[hold.origin] ?? "Chat"}
                      {hold.workflow_name ? ` · ${hold.workflow_name}` : ""} · held{" "}
                      {formatRelative(hold.created_at).replace(" ago", "")}
                    </span>
                    <strong>{BUDGET_RUN_LABEL}</strong>
                  </div>
                  {hold.conversation_id && (
                    <button
                      type="button"
                      className="ghost-button approval-open"
                      onClick={() => openConversation(hold.conversation_id)}
                    >
                      <ExternalLink size={13} />
                      Open thread
                    </button>
                  )}
                </div>
                <p className="inbox-hold-note">
                  Nothing to approve — the run resumes when a workspace owner raises
                  the ceiling under Settings › Usage &amp; budget.
                </p>
              </article>
            ))
          )}
        </div>
      )}

      {section === "mentions" && (
        <div className="approval-panel inbox-queue">
          {mentions.length === 0 ? (
            <div className="approval-empty">
              <div>
                <Check size={18} />
              </div>
              <strong>Nobody needs your eyes</strong>
              <p>Comments that @-mention you appear here until you resolve them.</p>
            </div>
          ) : (
            mentions.map((mention) => {
              const destination = mention.conversation_id
                ? "thread"
                : mention.document_id
                  ? "document"
                  : mention.dashboard_id
                    ? "dashboard"
                    : "";
              return (
                <article key={mention.id} className="approval-card">
                  <div className="approval-card-top">
                    <div className="tool-glyph">
                      <AtSign size={17} />
                    </div>
                    <div>
                      <span>
                        Mention · waiting{" "}
                        {formatRelative(mention.created_at).replace(" ago", "")}
                      </span>
                      <strong>{mention.title}</strong>
                    </div>
                    {destination && (
                      <button
                        type="button"
                        className="ghost-button approval-open"
                        onClick={() => openMention(mention)}
                      >
                        <ExternalLink size={13} />
                        Open {destination}
                      </button>
                    )}
                  </div>
                  {mention.body && <p className="inbox-hold-note">{mention.body}</p>}
                  <div className="decision-buttons">
                    <button
                      className="approve"
                      onClick={() =>
                        void resolveMention(mention.id).then(refreshFeed)
                      }
                    >
                      <Check size={15} />
                      Resolve
                    </button>
                  </div>
                </article>
              );
            })
          )}
        </div>
      )}

      {section === "runs" && (
        <div className="approval-panel inbox-queue">
          {runs.length === 0 ? (
            <p className="timeline-empty">No workflow runs have finished yet.</p>
          ) : (
            runs.map((run) => (
              <article key={run.id} className="approval-card inbox-run">
                <div className="approval-card-top">
                  <div className="tool-glyph">
                    <Clock size={17} />
                  </div>
                  <div>
                    <span>
                      {run.status === "failed" ? "Failed" : "Finished"} ·{" "}
                      {formatRelative(run.created_at)}
                    </span>
                    <strong>{run.workflow_name}</strong>
                  </div>
                  <span
                    className={
                      run.status === "failed" ? "status-pill failed" : "status-pill"
                    }
                  >
                    {run.status}
                  </span>
                </div>
                {run.error && <p className="inbox-hold-note">{run.error}</p>}
              </article>
            ))
          )}
        </div>
      )}

      {section === "history" && (
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
      )}
    </section>
  );
}
