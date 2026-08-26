"use client";

import {
  AtSign,
  Bell,
  Check,
  CircleDollarSign,
  Clock,
  ExternalLink,
  Inbox as InboxIcon,
  RefreshCw,
  TrendingUp,
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
import { RulesTable } from "./policies";
import { ProposalDiff } from "./proposal-diff";
import { formatRelative } from "./shared";
import {
  SNOOZE_KEY,
  addSnooze,
  nextMorning,
  parseSnoozes,
  pruneSnoozes,
  removeSnooze,
  snoozedIds,
  type SnoozeMap,
} from "./snooze";
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
  /**
   * The snooze map changed (snooze, unsnooze, or a prune). The shell's
   * waiting strip reads the same localStorage key, and a write here does not
   * re-render it on its own — without this callback a just-snoozed row keeps
   * ringing the doorbell beside the very tab that put it off.
   */
  onSnoozesChanged?: () => void;
  /** Flip a mention out of the waiting set; the caller re-reads the feed. */
  resolveMention: (notificationId: string) => Promise<void>;
  /** Jump to whatever a mention deep-links: its thread, document or dashboard. */
  openMention: (mention: InboxMention) => void;
  /** Flip a monitor alert out of the waiting set — for the whole room, since
   * an alert is '' -targeted and every member sees the same row. */
  resolveAlert: (notificationId: string) => Promise<void>;
  /** Jump to the Monitors view, where the tripped monitor is defined. */
  openMonitors: () => void;
  /** Flip a spend anomaly out of the waiting set — broadcast, like alerts. */
  resolveAnomaly: (notificationId: string) => Promise<void>;
  /** Jump to where this reader can act on spend: the admin usage panel for an
   * owner, the Agents view for everyone else. */
  openSpending: () => void;
  /** The signed-in member's user id — what splits the queue into "assigned to
   * you" vs the rest. "" until bootstrap's first read lands. */
  identityId: string;
  /** The workspace member list for the assignee control (the same list the
   * @-picker reads; every member may see it). */
  loadMembers: () => Promise<WorkspaceMember[]>;
  /** Route one approval to a member, or back to anyone with "". */
  assignApproval: (callId: string, userId: string) => Promise<boolean>;
};

type Section =
  | "approvals"
  | "snoozed"
  | "holds"
  | "mentions"
  | "alerts"
  | "anomalies"
  | "runs"
  | "history"
  | "rules";

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
  onSnooze,
  snoozedUntil,
  onUnsnooze,
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
  /** "Later": park this row until tomorrow morning. Only on the live queue. */
  onSnooze?: () => void;
  /** ISO wake time — set on rows rendered in the Later tab, with the way back. */
  snoozedUntil?: string;
  onUnsnooze?: () => void;
  selfId: string;
  members: WorkspaceMember[];
  assign: (callId: string, userId: string) => Promise<boolean>;
  /** True in the "assigned to others" group — their wait, not this reader's. */
  dimmed?: boolean;
}) {
  const [remember, setRemember] = useState(false);
  const [busy, setBusy] = useState(false);
  // The ask_user row's answer, collected HERE because the Inbox is exactly
  // the surface where a question parked overnight gets read — an Approve that
  // silently discarded the answer channel would make the agent guess at the
  // very question it stopped to ask.
  const [answer, setAnswer] = useState("");
  const asking = row.name === "ask_user";
  // These three calls never consult a standing grant server-side (the
  // decision endpoint excludes them from policy recording), so offering the
  // checkbox would promise a skip that cannot happen.
  const rememberable = !["ask_user", "exit_plan_mode", "__manual__"].includes(
    row.name,
  );
  const ref = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (focused) ref.current?.scrollIntoView({ block: "nearest" });
  }, [focused]);

  async function choose(decision: "approved" | "denied") {
    setBusy(true);
    try {
      const typed = answer.trim();
      await decide(
        asCall(row),
        decision,
        rememberable && remember,
        asking && decision === "approved" && typed ? { answer: typed } : undefined,
      );
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
      {asking && (
        <textarea
          className="ask-user-answer"
          aria-label="Answer the assistant's question"
          placeholder="Type your answer (optional — Approve sends it)"
          value={answer}
          rows={2}
          onChange={(event) => setAnswer(event.target.value)}
        />
      )}
      {rememberable && (
        <label className="approval-remember">
          <input
            type="checkbox"
            checked={remember}
            onChange={(event) => setRemember(event.target.checked)}
          />
          {/* The grant is personal — owner_id is the caller — so the copy must
              not promise the workspace. It lands in the Rules tab beside this
              queue, which is where it is taken back. */}
          Always allow {row.name} for me
          <span className="field-hint">Recorded under Rules</span>
        </label>
      )}
      {/* The wake time, said on the row: a queue of sleeping requests with no
          visible alarm is a queue the user has to trust from memory. */}
      {snoozedUntil && (
        <p className="inbox-snoozed-note">
          <Clock size={13} aria-hidden="true" />
          <span>
            Snoozed until{" "}
            {new Date(snoozedUntil).toLocaleString([], {
              weekday: "short",
              hour: "numeric",
              minute: "2-digit",
            })}
          </span>
          {onUnsnooze && (
            <button
              type="button"
              className="ghost-button"
              onClick={onUnsnooze}
              aria-label={`Unsnooze ${row.name}`}
            >
              Unsnooze
            </button>
          )}
        </p>
      )}
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
      {/* An assigned-away row's decision belongs to its assignee — the server
          would 409 either button, so neither is offered as pressable. */}
      <div className={onSnooze ? "decision-buttons with-snooze" : "decision-buttons"}>
        {onSnooze && (
          <button
            type="button"
            className="ghost-button"
            disabled={busy}
            aria-label={`Snooze ${row.name} until tomorrow morning`}
            onClick={onSnooze}
          >
            <Clock size={15} />
            Later
          </button>
        )}
        <button
          disabled={busy || dimmed}
          title={dimmed ? "Waiting on its assignee" : undefined}
          onClick={() => void choose("denied")}
        >
          <X size={15} />
          Deny
        </button>
        <button
          className="approve"
          disabled={busy || dimmed}
          title={dimmed ? "Waiting on its assignee" : undefined}
          onClick={() => void choose("approved")}
        >
          <Check size={15} />
          {asking && answer.trim()
            ? "Answer"
            : rememberable && remember
              ? "Approve"
              : "Approve once"}
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
  onSnoozesChanged,
  resolveMention,
  openMention,
  resolveAlert,
  openMonitors,
  resolveAnomaly,
  openSpending,
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
  // Rows a colleague is waiting on sit at the end of the flat array; only the
  // prefix is this reader's to act on — deciding an assigned-away row is a
  // guaranteed 409, so the keyboard walk stops before them and their buttons
  // are disabled.
  const actionableCount = buckets.mine.length + buckets.unassigned.length;
  const holds = feed?.budget_holds ?? [];
  const mentions = feed?.mentions ?? [];
  const alerts = feed?.alerts ?? [];
  const anomalies = feed?.anomalies ?? [];
  const runs = feed?.recent_runs ?? [];

  /**
   * "Later" state, read once on mount and written through on every change.
   * Snoozing splits the queue in two — the live rows and the sleeping ones —
   * without touching the server: a snooze is this user's attention schedule,
   * not a fact about the request, so the request itself stays parked exactly
   * as it was and the rail badge keeps counting it.
   */
  const [snoozes, setSnoozes] = useState<SnoozeMap>(() =>
    typeof window === "undefined"
      ? {}
      : parseSnoozes(window.localStorage.getItem(SNOOZE_KEY)),
  );
  function persistSnoozes(next: SnoozeMap) {
    setSnoozes(next);
    window.localStorage.setItem(SNOOZE_KEY, JSON.stringify(next));
    onSnoozesChanged?.();
  }
  // Recomputed per render, so an expired snooze returns its row to the queue
  // on the next refresh with no cleanup step to run.
  const asleep = snoozedIds(snoozes, new Date());
  const waiting = approvals.filter((row) => !asleep.has(row.id));
  const snoozed = approvals.filter((row) => asleep.has(row.id));

  // Each time the waiting set lands, drop snoozes for ids it no longer holds
  // (decided elsewhere) and ones already woken — the key must not grow one
  // dead pair per snooze for the life of the workspace. pruneSnoozes returns
  // the identical object when nothing changed, so no write-back loop.
  useEffect(() => {
    if (!feed) return;
    setSnoozes((current) => {
      const pruned = pruneSnoozes(
        current,
        new Set(feed.approvals.map((row) => row.id)),
        new Date(),
      );
      if (pruned === current) return current;
      window.localStorage.setItem(SNOOZE_KEY, JSON.stringify(pruned));
      onSnoozesChanged?.();
      return pruned;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [feed]);

  // J/K walk the queue, A/D decide the focused row — triage without a mouse.
  // Suppressed while anything focusable owns the keyboard, so typing a memo
  // into some other control cannot deny a write. Walks the UNSNOOZED rows,
  // because they are the only rows the approvals tab renders.
  useEffect(() => {
    if (section !== "approvals") return;
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target && /^(input|textarea|select)$/i.test(target.tagName)) return;
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.key === "j" || event.key === "J") {
        setFocusIndex((index) => Math.min(index + 1, Math.max(waiting.length - 1, 0)));
      } else if (event.key === "k" || event.key === "K") {
        setFocusIndex((index) => Math.max(index - 1, 0));
      } else if (event.key === "a" || event.key === "A" || event.key === "d" || event.key === "D") {
        const row = waiting[focusIndex];
        if (!row) return;
        // An assigned-away row's decision belongs to its assignee — the server
        // would 409 either button, so the keyboard walk refuses to decide it.
        if (row.assigned_to && row.assigned_to !== identityId) return;
        const decision = event.key.toLowerCase() === "a" ? "approved" : "denied";
        void decide(asCall(row), decision, false).then(refreshFeed);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [section, waiting, focusIndex, decide, refreshFeed]);

  useEffect(() => {
    setFocusIndex((index) => Math.min(index, Math.max(waiting.length - 1, 0)));
  }, [waiting.length]);

  const tabs: Array<{ id: Section; label: string; count?: number }> = [
    { id: "approvals", label: "Needs approval", count: waiting.length },
    { id: "snoozed", label: "Later", count: snoozed.length },
    { id: "holds", label: "Budget holds", count: holds.length },
    { id: "mentions", label: "Mentions", count: mentions.length },
    { id: "alerts", label: "Alerts", count: alerts.length },
    { id: "anomalies", label: "Spend", count: anomalies.length },
    { id: "runs", label: "Runs" },
    { id: "history", label: "History" },
    // The ledger the "always allow" checkbox above writes into — one
    // component, two mounts (here and Rules & policies), like ProposalDiff.
    { id: "rules", label: "Rules" },
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
          ) : waiting.length === 0 ? (
            <div className="approval-empty">
              <div>
                <Check size={18} />
              </div>
              <strong>Nothing needs you</strong>
              {/* Kept for the snoozed case only: it says why this list is
                  empty while items are in fact waiting, and where they went. */}
              {snoozed.length > 0 && (
                <p>Everything waiting is snoozed until morning, under Later.</p>
              )}
            </div>
          ) : (
            <>
              <p className="inbox-hint">
                Oldest first. <kbd>J</kbd>/<kbd>K</kbd> to move, <kbd>A</kbd> approve,{" "}
                <kbd>D</kbd> deny.
              </p>
              {(() => {
                const groups = [
                  {
                    label: "Assigned to you",
                    rows: buckets.mine.filter((row) => !asleep.has(row.id)),
                  },
                  {
                    label: "Unassigned",
                    rows: buckets.unassigned.filter((row) => !asleep.has(row.id)),
                  },
                  {
                    label: "Assigned to others",
                    rows: buckets.others.filter((row) => !asleep.has(row.id)),
                  },
                ] as const;
                const showHeader =
                  groups[0].rows.length + groups[2].rows.length > 0;
                let offset = 0;
                return groups.map((group) => {
                  const groupOffset = offset;
                  offset += group.rows.length;
                  if (group.rows.length === 0) return null;
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
                          focused={groupOffset + index === focusIndex}
                          onOpen={
                            row.conversation_id
                              ? () => openConversation(row.conversation_id)
                              : undefined
                          }
                          onDecided={refreshFeed}
                          onSnooze={() =>
                            persistSnoozes(
                              addSnooze(snoozes, row.id, nextMorning(new Date())),
                            )
                          }
                          selfId={identityId}
                          members={members}
                          assign={assignApproval}
                          dimmed={group.label === "Assigned to others"}
                        />
                      ))}
                    </div>
                  );
                });
              })()}
            </>
          )}
        </div>
      )}

      {section === "snoozed" && (
        <div className="approval-panel inbox-queue">
          {snoozed.length === 0 ? (
            <div className="approval-empty">
              <div>
                <Clock size={18} />
              </div>
              <strong>Nothing put off</strong>
            </div>
          ) : (
            snoozed.map((row) => (
              <ApprovalRow
                key={row.id}
                row={row}
                decide={decide}
                focused={false}
                onOpen={
                  row.conversation_id
                    ? () => openConversation(row.conversation_id)
                    : undefined
                }
                onDecided={refreshFeed}
                selfId={identityId}
                members={members}
                assign={assignApproval}
                snoozedUntil={snoozes[row.id]}
                onUnsnooze={() => persistSnoozes(removeSnooze(snoozes, row.id))}
              />
            ))
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
                      onClick={() => void resolveMention(mention.id)}
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

      {section === "alerts" && (
        <div className="approval-panel inbox-queue">
          {alerts.length === 0 ? (
            <div className="approval-empty">
              <div>
                <Check size={18} />
              </div>
              <strong>No monitor has tripped</strong>
            </div>
          ) : (
            alerts.map((alert) => (
              <article key={alert.id} className="approval-card">
                <div className="approval-card-top">
                  <div className="tool-glyph">
                    <Bell size={17} />
                  </div>
                  <div>
                    <span>
                      Monitor alert · waiting{" "}
                      {formatRelative(alert.created_at).replace(" ago", "")}
                    </span>
                    <strong>{alert.title}</strong>
                  </div>
                  {alert.monitor_id && (
                    <button
                      type="button"
                      className="ghost-button approval-open"
                      onClick={openMonitors}
                    >
                      <ExternalLink size={13} />
                      Open monitors
                    </button>
                  )}
                </div>
                {alert.body && <p className="inbox-hold-note">{alert.body}</p>}
                <div className="decision-buttons">
                  <button
                    className="approve"
                    onClick={() => void resolveAlert(alert.id)}
                  >
                    <Check size={15} />
                    Resolve
                  </button>
                </div>
              </article>
            ))
          )}
        </div>
      )}

      {section === "anomalies" && (
        <div className="approval-panel inbox-queue">
          {anomalies.length === 0 ? (
            <div className="approval-empty">
              <div>
                <Check size={18} />
              </div>
              <strong>Spending looks usual</strong>
            </div>
          ) : (
            anomalies.map((anomaly) => (
              <article key={anomaly.id} className="approval-card">
                <div className="approval-card-top">
                  <div className="tool-glyph">
                    <TrendingUp size={17} />
                  </div>
                  <div>
                    <span>
                      Spend anomaly · flagged{" "}
                      {formatRelative(anomaly.created_at).replace(" ago", "")}
                    </span>
                    <strong>{anomaly.title}</strong>
                  </div>
                  <button
                    type="button"
                    className="ghost-button approval-open"
                    onClick={openSpending}
                  >
                    <ExternalLink size={13} />
                    Review spending
                  </button>
                </div>
                {anomaly.body && <p className="inbox-hold-note">{anomaly.body}</p>}
                <div className="decision-buttons">
                  <button
                    className="approve"
                    onClick={() => void resolveAnomaly(anomaly.id)}
                  >
                    <Check size={15} />
                    Resolve
                  </button>
                </div>
              </article>
            ))
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

      {section === "rules" && <RulesTable />}

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
