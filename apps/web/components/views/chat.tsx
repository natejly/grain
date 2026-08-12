"use client";

import {
  ArrowUp,
  Ban,
  Check,
  ChevronRight,
  Copy,
  FileText,
  Paperclip,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  Square,
  Wrench,
  X,
  Zap,
} from "lucide-react";
import type {
  AgentInfo,
  AgentToolCall,
  ApprovalMode,
  Board,
  Citation,
  CitationCheck,
  GeneratedApp,
  Message,
  Source,
} from "@workspace/api-client";
import { FormEvent, useEffect, useState } from "react";
import { api } from "../api";
import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkMath from "remark-math";
import { ChatDashboardEmbeds } from "../chat-dashboard-embed";
import { ArtifactImages } from "../source-image";
import { autoApprovedCalls, isBypass } from "./approval-format";
import { ApprovalModeControl, BypassIndicator } from "./approval-mode";
import { BudgetHold } from "./budget";
import type { BudgetPark } from "./budget-format";
import { describeCitationCheck } from "./citation-format";
import { TODO_TOOLS, listForTodoCall } from "./todo-format";
import { TodoChecklist, type TodoOps } from "./todos";

export type ToolDecision = (
  call: AgentToolCall,
  decision: "approved" | "denied",
  remember: boolean,
) => Promise<void>;

export type ChatViewProps = {
  messages: Message[];
  sources: Source[];
  agentCalls: AgentToolCall[];
  /**
   * The workspace's generated apps, so a message that links to a published one
   * can show it rather than only naming it. Passed in rather than fetched here
   * because the embed must only ever appear for an app the shell already knows
   * about — see `chat-dashboard-embed.tsx`.
   */
  apps: GeneratedApp[];
  draft: string;
  setDraft: (value: string) => void;
  activeRun: string | null;
  runStatus: string;
  /**
   * Set while the streamed run is parked on the spend ceiling. It is not an
   * approval and has no `AgentToolCall` behind it, so it gets its own panel
   * rather than a tool card with different words in it.
   */
  budgetPark: BudgetPark | null;
  submitPrompt: (event?: FormEvent) => Promise<void>;
  cancelActiveRun: () => Promise<void>;
  regenerate: () => Promise<void>;
  decideAgentCall: ToolDecision;
  openCitation: (citation: Citation) => Promise<void>;
  /**
   * Add a source. Omitted where there is nowhere to go: the panel beside a
   * document would have to leave the document to reach the Knowledge view, so
   * it shows no paperclip rather than one that discards what you were doing.
   */
  onAttach?: () => void;
  /**
   * This thread's approval mode, and the way to change it.
   *
   * Optional because the mode belongs to a *conversation*, and this component
   * is also mounted where there is no thread of one's own to govern. Where it
   * is absent no control renders — rather than a control that reads
   * "Ask before writes" while governing nothing.
   */
  approval?: {
    mode: ApprovalMode;
    setMode: (mode: ApprovalMode) => Promise<void>;
    conversationId: string | null;
    conversationTitle: string;
  };
  /**
   * The workspace's todo lists, so a turn that touched one can show it as
   * checkboxes here instead of sending the reader to another page.
   */
  todos?: { lists: Board[]; ops: TodoOps };
  endRef: React.RefObject<HTMLDivElement | null>;
  /** "" means the workspace default agent; otherwise an authored agent's id. */
  // Optional: the document panel mounts ChatView without an agent picker,
  // the same way it mounts without `onAttach` or `approval`.
  selectedAgentId?: string;
  onSelectAgent?: (agentId: string) => void;
  /**
   * Per-turn model / reasoning-effort / fast overrides for the composer, with
   * the deployment's allow-lists to draw from. Optional and grouped for the same
   * reason as `approval`: the document panel mounts ChatView without them and so
   * renders no such controls.
   */
  turnControls?: {
    models: string[];
    efforts: string[];
    model: string;
    setModel: (value: string) => void;
    effort: string;
    setEffort: (value: string) => void;
    fast: boolean;
    setFast: (value: boolean) => void;
  };
};

/**
 * Who answers the next message. Fetches the enabled agents itself — the list
 * is small, only this control wants it, and putting it in the workspace hook
 * would load it for every user who never opens the menu. One agent means no
 * choice, so the control renders nothing and the composer stays as it was.
 */
function AgentSelect({
  selectedAgentId,
  onSelectAgent,
}: {
  selectedAgentId: string;
  onSelectAgent: (agentId: string) => void;
}) {
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  useEffect(() => {
    let cancelled = false;
    void api
      .listAgents()
      .then((rows) => {
        if (!cancelled) setAgents(rows.filter((row) => row.enabled));
      })
      .catch(() => undefined); // the default agent still answers
    return () => {
      cancelled = true;
    };
  }, []);
  if (agents.length < 2) return null;
  return (
    <select
      className="agent-select"
      value={selectedAgentId}
      onChange={(event) => onSelectAgent(event.target.value)}
      aria-label="Agent"
    >
      <option value="">Default agent</option>
      {agents.map((agent) => (
        <option key={agent.id} value={agent.id}>
          {agent.name}
        </option>
      ))}
    </select>
  );
}

/**
 * The per-turn model, reasoning effort and fast shortcut, drawn from the
 * deployment's allow-lists. Each part renders only when the deployment offers
 * choices for it — a scripted provider with no models or efforts shows nothing.
 * "Fast" is the low-effort shortcut, so it disables the effort dropdown while on
 * (the backend ignores the effort under fast) and pairs with that dropdown.
 */
function TurnControls({
  models,
  efforts,
  model,
  setModel,
  effort,
  setEffort,
  fast,
  setFast,
  disabled,
}: NonNullable<ChatViewProps["turnControls"]> & { disabled: boolean }) {
  return (
    <>
      {models.length > 0 && (
        <select
          className="composer-select"
          value={model}
          onChange={(event) => setModel(event.target.value)}
          disabled={disabled}
          aria-label="Model"
        >
          <option value="">Default model</option>
          {models.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
      )}
      {efforts.length > 0 && (
        <>
          <select
            className="composer-select"
            value={effort}
            onChange={(event) => setEffort(event.target.value)}
            disabled={disabled || fast}
            aria-label="Reasoning effort"
          >
            {efforts.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
          <button
            type="button"
            className={fast ? "composer-toggle on" : "composer-toggle"}
            onClick={() => setFast(!fast)}
            disabled={disabled}
            aria-pressed={fast}
            title="Fast: skip extended reasoning"
          >
            <Zap size={13} aria-hidden="true" /> Fast
          </button>
        </>
      )}
    </>
  );
}

function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="copy-button"
      aria-label={label}
      onClick={() => {
        void navigator.clipboard?.writeText(value).then(() => {
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1200);
        });
      }}
    >
      {copied ? <Check size={13} /> : <Copy size={13} />}
      {copied ? "Copied" : "Copy"}
    </button>
  );
}

/** Fenced code blocks get a copy affordance; inline code stays inline. */
function MarkdownBody({ content }: { content: string }) {
  return (
    <ReactMarkdown
      // Chat renders maths for the same reason Documents does, and it is the
      // surface where people actually ask for it: an assistant that answers a
      // calculus question in a chat message was printing the TeX source. The
      // stylesheet is imported globally in app/layout.tsx rather than here,
      // because importing it from a view means it only loads if you have
      // visited that view — which is how this ended up rendering unstyled.
      remarkPlugins={[remarkMath]}
      rehypePlugins={[rehypeKatex]}
      components={{
        pre({ children }) {
          const text = extractText(children);
          return (
            <div className="code-block">
              <div className="code-block-bar">
                <CopyButton value={text} label="Copy code" />
              </div>
              <pre>{children}</pre>
            </div>
          );
        },
      }}
    >
      {content}
    </ReactMarkdown>
  );
}

/** Pull the raw text out of a rendered <pre> subtree so Copy gets the source. */
function extractText(node: React.ReactNode): string {
  if (node === null || node === undefined || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(extractText).join("");
  const element = node as { props?: { children?: React.ReactNode } };
  if (element.props) return extractText(element.props.children);
  return "";
}

/**
 * Render a unified diff with per-line colouring. Anything that isn't a diff
 * (a board move reads "Move X from Todo to Done") falls through as plain text.
 */
function ProposalPreview({ preview }: { preview: string }) {
  const lines = preview.split("\n");
  const isDiff = lines.some((line) => line.startsWith("@@"));
  if (!isDiff) {
    return <div className="proposal-note">{preview}</div>;
  }
  return (
    <div className="diff">
      {lines.map((line, index) => {
        let kind = "ctx";
        if (line.startsWith("+++") || line.startsWith("---")) kind = "file";
        else if (line.startsWith("@@")) kind = "hunk";
        else if (line.startsWith("+")) kind = "add";
        else if (line.startsWith("-")) kind = "del";
        return (
          <div key={index} className={`diff-line ${kind}`}>
            {line || " "}
          </div>
        );
      })}
    </div>
  );
}

/**
 * The citation validator's verdict on an answer, where the answer is.
 *
 * Not a decoration. `services/citations.py` is what backs the product's claim
 * that a `[n]` in an answer names a passage that really was retrieved, and its
 * report went to an audit row for a year — so a fabricated `[4]` in an answer
 * built from three passages reached the reader looking exactly like a real
 * citation, with the checker's objection filed where nobody looks.
 */
function CitationVerdictNote({ report }: { report: CitationCheck }) {
  const verdict = describeCitationCheck(report);
  if (!verdict) return null;
  const Icon = verdict.tone === "clean" ? ShieldCheck : ShieldAlert;
  return (
    <div
      className={`citation-check ${verdict.tone}`}
      // Announced only for the one tone that is a defect. An uncited passage
      // is not a contract violation — the validator says so — and interrupting
      // a screen reader for every tool-driven turn is how a real alert gets
      // tuned out before it ever fires.
      role={verdict.tone === "fabricated" ? "alert" : undefined}
    >
      <Icon size={14} aria-hidden="true" />
      <div>
        <strong>{verdict.title}</strong>
        <span>{verdict.detail}</span>
      </div>
    </div>
  );
}

function prettyArguments(raw: string): string {
  if (!raw || raw === "{}") return "";
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

/**
 * What a finished call's status line says.
 *
 * "denied" gets a word and an icon of its own because it has to survive the
 * bypass: under `auto_writes` a tool a policy forbids is still refused, and a
 * thread where everything else sails through is exactly where a refusal
 * rendered as one more grey word would be read as success.
 */
function ToolStatus({ call }: { call: AgentToolCall }) {
  const latency = call.latency_ms > 0 ? ` · ${call.latency_ms}ms` : "";
  if (call.status === "proposed") return <span className="tool-status">Needs approval</span>;
  if (call.status === "denied") {
    return (
      <span className="tool-status denied">
        <Ban size={12} aria-hidden="true" /> Denied — not run{latency}
      </span>
    );
  }
  return (
    <span className="tool-status">
      {call.approved_by_mode ? (
        // The trail, on the call itself. `approved_by_mode` is set by the
        // server only where the mode changed the answer, so this badge never
        // appears on a call a standing policy would have allowed anyway.
        <span className="auto-approved">
          <Zap size={12} aria-hidden="true" /> Auto-approved
        </span>
      ) : null}
      {call.status}
      {latency}
    </span>
  );
}

function ToolCallCard({
  call,
  decide,
  todos,
}: {
  call: AgentToolCall;
  decide: ToolDecision;
  todos?: { lists: Board[]; ops: TodoOps };
}) {
  const [expanded, setExpanded] = useState(false);
  const [remember, setRemember] = useState(false);
  const [busy, setBusy] = useState(false);
  const pending = call.status === "proposed";
  const args = prettyArguments(call.arguments_json);
  // A pending write shows what it will do; the raw arguments stay one click away.
  const preview = call.proposal_preview;
  /**
   * The list this call was about, once it has actually happened.
   *
   * Only for a call that ran: a *proposed* `todo_check` has not ticked
   * anything, and drawing the list under the card that is still asking would
   * show the item unticked beside a preview saying it is about to be ticked —
   * two states of the same thing, side by side, one of them wrong.
   */
  const touchedList =
    todos && call.status === "succeeded" ? listForTodoCall(call, todos.lists) : null;

  async function choose(decision: "approved" | "denied") {
    setBusy(true);
    try {
      await decide(call, decision, remember);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={`tool-card ${call.status}`}>
      <button
        type="button"
        className="tool-card-head"
        onClick={() => setExpanded((value) => !value)}
        aria-expanded={expanded}
      >
        <ChevronRight size={14} className={expanded ? "chev open" : "chev"} />
        <Wrench size={14} />
        <span className="tool-name">{call.name}</span>
        <ToolStatus call={call} />
      </button>
      {preview && <ProposalPreview preview={preview} />}
      {touchedList && todos && (
        <TodoChecklist
          list={touchedList}
          ops={todos.ops}
          compact
          caption={
            call.name === "todo_check"
              ? "The assistant ticked an item off this list."
              : "The assistant added to this list."
          }
        />
      )}
      {/* Outside the disclosure, deliberately. A chart behind a closed triangle
          is as invisible as a chart that was never rendered — which is the bug
          this is fixing, arriving one click later. */}
      <ArtifactImages artifacts={call.artifacts} label={call.name} />
      {expanded && (
        <div className="tool-card-body">
          {args && (
            <>
              <div className="tool-label">Arguments</div>
              <pre>{args}</pre>
            </>
          )}
          {call.result_preview && (
            <>
              <div className="tool-label">Result</div>
              <pre>{call.result_preview}</pre>
            </>
          )}
          {call.error && <div className="tool-error">{call.error}</div>}
        </div>
      )}
      {pending && (
        <div className="tool-card-approval">
          <label className="remember">
            <input
              type="checkbox"
              checked={remember}
              onChange={(event) => setRemember(event.target.checked)}
            />
            Always allow {call.name}
          </label>
          <div className="tool-card-actions">
            <button
              type="button"
              className="ghost-button"
              disabled={busy}
              onClick={() => void choose("denied")}
            >
              <X size={14} /> Deny
            </button>
            <button
              type="button"
              className="primary-button"
              disabled={busy}
              onClick={() => void choose("approved")}
            >
              <Check size={14} /> Approve
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export function ChatView({
  messages,
  sources,
  agentCalls,
  apps,
  draft,
  setDraft,
  activeRun,
  runStatus,
  budgetPark,
  submitPrompt,
  cancelActiveRun,
  regenerate,
  decideAgentCall,
  openCitation,
  onAttach,
  approval,
  todos,
  endRef,
  selectedAgentId,
  onSelectAgent,
  turnControls,
}: ChatViewProps) {
  // Tool calls belong to a run, and every message carries its run_id, so they
  // stay anchored to the right turn after a reload rather than only while live.
  const callsForRun = (runId: string) =>
    agentCalls.filter((call) => call.run_id === runId);
  const bypassed = Boolean(approval && isBypass(approval.mode));
  /**
   * Which card in a turn gets the checklist: the last one that touched a list.
   *
   * A turn that adds three items makes three calls, and every one of them would
   * draw the same finished list — three identical checklists stacked, the first
   * two of which claim to be the state after one item. One turn, one list, at
   * the end of it, where it is true.
   */
  const checklistCallId = (calls: AgentToolCall[]): string =>
    calls.filter((call) => call.status === "succeeded" && TODO_TOOLS.includes(call.name)).at(-1)
      ?.id ?? "";
  const lastAssistant = [...messages].reverse().find((item) => item.role === "assistant");
  // A live card is only a duplicate once its run has an assistant message to
  // hang under. The user's own message carries the same run_id, so matching on
  // run_id alone hid every approval card for the whole turn.
  const liveCalls = activeRun
    ? callsForRun(activeRun).filter(
        (call) =>
          !messages.some(
            (message) =>
              message.role === "assistant" && message.run_id === call.run_id,
          ),
      )
    : [];

  return (
    <section className="chat-layout">
      <div className={`message-scroll ${messages.length === 0 ? "empty" : ""}`}>
        {messages.length > 0 && (
          <div className="message-column">
            {messages.map((message) => (
              <article key={message.id} className={`message ${message.role}`}>
                {message.role === "assistant" &&
                  (() => {
                    const calls = callsForRun(message.run_id);
                    const showChecklist = checklistCallId(calls);
                    return calls.map((call) => (
                      <ToolCallCard
                        key={call.id}
                        call={call}
                        decide={decideAgentCall}
                        todos={call.id === showChecklist ? todos : undefined}
                      />
                    ));
                  })()}
                <div className="message-author">
                  {message.role === "user" ? (
                    <div className="tiny-avatar">U</div>
                  ) : (
                    <div className="assistant-mark">A</div>
                  )}
                  <span>{message.role === "user" ? "You" : "Assistant"}</span>
                  {message.role === "assistant" && message.content && (
                    <CopyButton value={message.content} label="Copy message" />
                  )}
                </div>
                <div className="message-body">
                  {message.role === "assistant" ? (
                    <MarkdownBody content={message.content} />
                  ) : (
                    <p>{message.content}</p>
                  )}
                </div>
                {/* Unconditionally rendered, and that is the point: it returns
                    null when the message names no app, so its position in this
                    subtree never changes. A conditional here would take the
                    iframe out of the tree and put it back — which reloads the
                    frame — the first time a streamed message crossed the line
                    between naming an app and not. */}
                <ChatDashboardEmbeds content={message.content} apps={apps} />
                {message.citation_report && (
                  <CitationVerdictNote report={message.citation_report} />
                )}
                {message.citations.length > 0 && (
                  <div className="citations">
                    {message.citations.map((citation, index) => (
                      <button key={citation.chunk_id} onClick={() => void openCitation(citation)}>
                        <FileText size={13} />
                        <span>[{index + 1}]</span>
                        {citation.filename}
                      </button>
                    ))}
                  </div>
                )}
              </article>
            ))}
            {liveCalls.map((call) => (
              <ToolCallCard
                key={call.id}
                call={call}
                decide={decideAgentCall}
                todos={call.id === checklistCallId(liveCalls) ? todos : undefined}
              />
            ))}
            {/* Sits where a tool card would, and is deliberately not one: the
                run parked before it asked the model anything, so there is no
                proposed call and an approve/deny pair would decide nothing. */}
            {activeRun && budgetPark && (
              <BudgetHold park={budgetPark} menuId="chat-spend-ceiling" />
            )}
            {activeRun && runStatus && (
              <div className="run-status">
                <span className="thinking-dots">
                  <i />
                  <i />
                  <i />
                </span>
                {runStatus}
              </div>
            )}
            {!activeRun && lastAssistant && (
              <div className="turn-actions">
                <button type="button" className="ghost-button" onClick={() => void regenerate()}>
                  <RefreshCw size={14} /> Regenerate
                </button>
              </div>
            )}
            <div ref={endRef} />
          </div>
        )}
      </div>

      <div className={bypassed ? "composer-zone bypassed" : "composer-zone"}>
        {/* In the composer zone rather than in the transcript, and that is the
            whole design: a transcript scrolls, so a warning placed in one is a
            warning you can leave behind above the fold. This cannot be
            scrolled away from while the bypass is on. */}
        {approval && bypassed && (
          <BypassIndicator
            conversationTitle={approval.conversationTitle}
            approved={autoApprovedCalls(agentCalls, approval.conversationId)}
            stop={() => approval.setMode("ask_writes")}
          />
        )}
        <form className="composer" onSubmit={(event) => void submitPrompt(event)}>
          <textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                void submitPrompt();
              }
            }}
            // The hint is the empty workspace's only instruction, so it stays
            // visible; the accessible name is stable ("Message") so tests and
            // screen readers do not depend on whether a source is indexed yet.
            placeholder={
              sources.some((source) => source.status === "ready")
                ? "Ask your workspace…"
                : "Upload a source, then ask a question…"
            }
            rows={1}
            disabled={Boolean(activeRun)}
            aria-label="Message"
          />
          <div className="composer-tools">
            {onAttach && (
              <button
                type="button"
                onClick={onAttach}
                title="Add a source"
                aria-label="Add a source"
              >
                <Paperclip size={17} />
              </button>
            )}
            {onSelectAgent && (
              <AgentSelect
                selectedAgentId={selectedAgentId ?? ""}
                onSelectAgent={onSelectAgent}
              />
            )}
            {turnControls && (
              <TurnControls {...turnControls} disabled={Boolean(activeRun)} />
            )}
            {approval && (
              <ApprovalModeControl mode={approval.mode} setMode={approval.setMode} />
            )}
            <span className="composer-spacer" />
            {activeRun ? (
              <button
                type="button"
                className="send-button stop"
                onClick={() => void cancelActiveRun()}
                aria-label="Stop generating"
              >
                <Square size={15} />
              </button>
            ) : (
              <button
                className="send-button"
                type="submit"
                disabled={!draft.trim()}
                aria-label="Send message"
              >
                <ArrowUp size={18} />
              </button>
            )}
          </div>
        </form>
      </div>
    </section>
  );
}
