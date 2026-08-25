"use client";

import {
  Check,
  CircleDollarSign,
  Clock,
  ExternalLink,
  Inbox as InboxIcon,
  RefreshCw,
  X,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type {
  AgentToolCall,
  AuditEvent,
  InboxApproval,
  InboxFeed,
} from "@workspace/api-client";
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
};

type Section = "approvals" | "snoozed" | "holds" | "runs" | "history" | "rules";

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

  return (
    <article
      ref={ref}
      className={focused ? "approval-card focused" : "approval-card"}
      data-call-id={row.id}
    >
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
}: InboxViewProps) {
  const [section, setSection] = useState<Section>("approvals");
  const [focusIndex, setFocusIndex] = useState(0);

  const approvals = feed?.approvals ?? [];
  const holds = feed?.budget_holds ?? [];
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
              <p>
                {snoozed.length > 0
                  ? "Everything waiting is snoozed — it lives under Later until morning."
                  : "Tool calls waiting for approval appear here, whatever started them."}
              </p>
            </div>
          ) : (
            <>
              <p className="inbox-hint">
                Oldest first. <kbd>J</kbd>/<kbd>K</kbd> to move, <kbd>A</kbd> approve,{" "}
                <kbd>D</kbd> deny.
              </p>
              {waiting.map((row, index) => (
                <ApprovalRow
                  key={row.id}
                  row={row}
                  decide={decide}
                  focused={index === focusIndex}
                  onOpen={
                    row.conversation_id
                      ? () => openConversation(row.conversation_id)
                      : undefined
                  }
                  onDecided={refreshFeed}
                  onSnooze={() =>
                    persistSnoozes(addSnooze(snoozes, row.id, nextMorning(new Date())))
                  }
                />
              ))}
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
              <p>Approvals you snooze wait here until their morning comes.</p>
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
