"use client";

import {
  ArrowUp,
  Check,
  ChevronRight,
  Copy,
  FileText,
  Paperclip,
  RefreshCw,
  Square,
  Wrench,
  X,
} from "lucide-react";
import type { AgentToolCall, Citation, Message, Source } from "@workspace/api-client";
import { FormEvent, useState } from "react";
import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkMath from "remark-math";

export type ToolDecision = (
  call: AgentToolCall,
  decision: "approved" | "denied",
  remember: boolean,
) => Promise<void>;

export type ChatViewProps = {
  messages: Message[];
  sources: Source[];
  agentCalls: AgentToolCall[];
  draft: string;
  setDraft: (value: string) => void;
  activeRun: string | null;
  runStatus: string;
  submitPrompt: (event?: FormEvent) => Promise<void>;
  cancelActiveRun: () => Promise<void>;
  regenerate: () => Promise<void>;
  decideAgentCall: ToolDecision;
  openCitation: (citation: Citation) => Promise<void>;
  onAttach: () => void;
  endRef: React.RefObject<HTMLDivElement | null>;
};

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

function prettyArguments(raw: string): string {
  if (!raw || raw === "{}") return "";
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

function ToolCallCard({
  call,
  decide,
}: {
  call: AgentToolCall;
  decide: ToolDecision;
}) {
  const [expanded, setExpanded] = useState(false);
  const [remember, setRemember] = useState(false);
  const [busy, setBusy] = useState(false);
  const pending = call.status === "proposed";
  const args = prettyArguments(call.arguments_json);
  // A pending write shows what it will do; the raw arguments stay one click away.
  const preview = call.proposal_preview;

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
        <span className="tool-status">
          {pending ? "Needs approval" : call.status}
          {call.latency_ms > 0 ? ` · ${call.latency_ms}ms` : ""}
        </span>
      </button>
      {preview && <ProposalPreview preview={preview} />}
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
  draft,
  setDraft,
  activeRun,
  runStatus,
  submitPrompt,
  cancelActiveRun,
  regenerate,
  decideAgentCall,
  openCitation,
  onAttach,
  endRef,
}: ChatViewProps) {
  // Tool calls belong to a run, and every message carries its run_id, so they
  // stay anchored to the right turn after a reload rather than only while live.
  const callsForRun = (runId: string) =>
    agentCalls.filter((call) => call.run_id === runId);
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
        {messages.length === 0 ? (
          <div className="welcome">
            <h1>New conversation</h1>
            <p>Ask a question about your sources.</p>
          </div>
        ) : (
          <div className="message-column">
            {messages.map((message) => (
              <article key={message.id} className={`message ${message.role}`}>
                {message.role === "assistant" &&
                  callsForRun(message.run_id).map((call) => (
                    <ToolCallCard key={call.id} call={call} decide={decideAgentCall} />
                  ))}
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
              <ToolCallCard key={call.id} call={call} decide={decideAgentCall} />
            ))}
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

      <div className="composer-zone">
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
            placeholder={
              sources.some((source) => source.status === "ready")
                ? "Ask your workspace…"
                : "Upload a source, then ask a question…"
            }
            rows={1}
            disabled={Boolean(activeRun)}
          />
          <div className="composer-tools">
            <button
              type="button"
              onClick={onAttach}
              title="Add a source"
              aria-label="Add a source"
            >
              <Paperclip size={17} />
            </button>
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
