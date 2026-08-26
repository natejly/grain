"use client";

import {
  ArrowUp,
  Ban,
  Bot,
  Brain,
  Check,
  ChevronRight,
  Copy,
  FileText,
  GitFork,
  Paperclip,
  Pencil,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  Square,
  Terminal,
  Undo2,
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
  Skill,
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
import {
  commandDescription,
  matchCommands,
  type BuiltinCommand,
} from "./commands";
import {
  ApprovalModeControl,
  BypassIndicator,
  UnrestrictedIndicator,
} from "./approval-mode";
import { BudgetHold } from "./budget";
import type { BudgetPark } from "./budget-format";
import { describeCitationCheck } from "./citation-format";
import { ProposalDiff } from "./proposal-diff";
import { DashboardPinBar, type DashboardPinning } from "./dashboard-pin-bar";
import { baseName, isTabular, senderInitial, senderIsViewer, senderLabel } from "./shared";
import { steerStripVisible } from "./steer-format";
import { TODO_TOOLS, listForTodoCall } from "./todo-format";
import { TodoChecklist, type TodoOps } from "./todos";

export type ToolDecision = (
  call: AgentToolCall,
  decision: "approved" | "denied",
  remember: boolean,
  /**
   * The human's typed contribution to the approval, when the card collects
   * one — today the `ask_user` card's answer, `{ answer: string }`. Rides the
   * decision's amendment channel; the server merges it into the executor's
   * arguments without rewriting the model's own `arguments_json`.
   */
  inputs?: Record<string, unknown>,
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
  /**
   * Runs the prompt-injection screen flagged. A turn whose run is in here gets a
   * visible mark so the reader knows untrusted content tried to steer the answer
   * and — in enforce mode — was forced to ask before every tool call. Optional
   * and defaulting to none: the panel beside a document does not track it.
   */
  flaggedRuns?: string[];
  /**
   * True when this thread is shared with the workspace, so each message is
   * labelled with the member it is attributed to (`message.sender_name`) rather
   * than a bare "You". Absent/false on a personal thread, where every message is
   * the caller's and a name would be noise.
   */
  sharedThread?: boolean;
  submitPrompt: (event?: FormEvent) => Promise<void>;
  cancelActiveRun: () => Promise<void>;
  /**
   * Add a mid-turn note to the run that is streaming right now. Resolves true
   * when the note was delivered — the strip keeps the draft on failure, since
   * an error banner far from the input is not a place to lose a sentence to.
   * Optional: panels that mount ChatView without it show no steer strip, and
   * the strip hides while the run is parked in ANY way — on an approval, on
   * the spend ceiling, or with its card decided elsewhere — because a parked
   * run wants a decision, not more words, and the server refuses with a 409.
   */
  steer?: (content: string) => Promise<boolean>;
  regenerate: () => Promise<void>;
  /**
   * Rewrite one of the viewer's own prompts and re-run the thread from there.
   * The edit is a truncation — everything after the message is deleted server-
   * side — so the pencil rides only messages `senderIsViewer` allows. Resolves
   * true when the edit was accepted; false keeps the editor open, because the
   * rewritten words are not the error banner's to lose. Optional: the subject
   * panels mount ChatView without it and show no pencil.
   */
  editMessage?: (messageId: string, content: string) => Promise<boolean>;
  /**
   * The signed-in member's id, matched against `message.sender_id` so a shared
   * thread offers the pencil only on the viewer's own prompts. Optional with
   * `editMessage`; absent means no shared-thread message is editable.
   */
  viewerId?: string;
  decideAgentCall: ToolDecision;
  openCitation: (citation: Citation) => Promise<void>;
  /**
   * Add a source without leaving the conversation. The paperclip used to
   * navigate to the Sources page — an attach button that teleported you away
   * from the thread you were attaching *for* — so it is a popover now: the
   * file uploads in place, lands in workspace knowledge, and the thread can
   * cite it as soon as it is indexed. Omitted on the panels beside a document
   * or dashboard, which show no paperclip at all.
   */
  attach?: {
    upload: (files: FileList | File[]) => Promise<Source | null>;
    uploading: boolean;
    /**
     * Turn the uploaded file into a dataset too. Offered — and preselected —
     * only for tabular files, because a CSV attached to a chart question that
     * lands as prose chunks answers retrieval and not the chart.
     */
    createDataset?: (name: string, sourceId: string) => Promise<void>;
  };
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
   * Whether an empty transcript teaches with starter cards. Defaults to
   * following `approval` (the rail chat and extra panes teach); the subject
   * panels pass false — they now carry the approval control too, but an empty
   * panel beside a document is scoped to that document and the product-verbs
   * lesson would be the wrong lesson there.
   */
  showStarter?: boolean;
  /**
   * The deployment is running with `DEV_UNRESTRICTED_AGENT`: nothing parks and
   * the per-subject tool scoping is off. Carries the thread's conversation id
   * because the indicator names what the bypass actually let through, and the
   * call list this view is handed is workspace-wide on the side panels.
   *
   * Separate from `approval` on purpose: the panels have no mode *control* —
   * there is nothing here for a user to change — but they still have to show
   * the warning, and a surface that could be in this state without saying so is
   * the exact failure the indicator exists to prevent.
   */
  unrestricted?: { conversationId: string | null };
  /**
   * The workspace's todo lists, so a turn that touched one can show it as
   * checkboxes here instead of sending the reader to another page.
   */
  todos?: { lists: Board[]; ops: TodoOps };
  /**
   * The finish-the-job bar on a chart-shaped tool card: pin the dashboard the
   * turn authored without leaving the thread, or — for a chart that is only a
   * picture — ask the agent for the pinnable version. Optional like `todos`:
   * the panels beside a document or dashboard mount ChatView without it and
   * show no bar.
   */
  pinning?: DashboardPinning;
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
    /**
     * The Thinking toggle — stream reasoning summaries as a live trail.
     * Optional: a surface that offers no toggle simply never shows one.
     */
    thinking?: boolean;
    setThinking?: (value: boolean) => void;
  };
  /** The live thinking trail streamed by the active run; "" between runs. */
  thinking?: string;
  /**
   * The composer's slash-command picker: the skill attached to the next turn,
   * the values for its declared args, and the ways to change them. Optional and
   * grouped like `approval`/`turnControls` — the document panel mounts ChatView
   * without it and so shows no picker. The attachment is per-turn state the
   * caller clears once the send lands; here we only read and edit it.
   */
  skills?: {
    attached: Skill | null;
    argValues: Record<string, unknown>;
    attach: (skill: Skill) => void;
    detach: () => void;
    setArg: (name: string, value: unknown) => void;
  };
  /**
   * Branch a new thread from everything said up to one message. Optional and
   * only passed by the rail chat: the panels beside a document or dashboard
   * hold a subject's one thread, where a fork would have nowhere to go — no
   * prop, no button, exactly like `attach` and `approval`.
   */
  fork?: (messageId: string) => Promise<void>;
  /**
   * Revert a finished run's writes from its recorded checkpoints. Optional
   * for the same reason `fork` is: only the rail chat passes it, and the
   * handler owns the confirm and the skipped-summary notice.
   */
  undo?: (runId: string) => Promise<void>;
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
  // A remembered agent that no longer exists (deleted, disabled) must not
  // stick: the select would render blank while every send still carried the
  // dead id into "Agent is not available". Clearing through onSelectAgent
  // also clears the THREAD's remembered default, so the thread self-heals
  // rather than resurrecting the ghost on every reopen. Skipped while the
  // list is empty — a failed fetch is not evidence the agent is gone.
  useEffect(() => {
    if (agents.length === 0 || !selectedAgentId) return;
    if (!agents.some((agent) => agent.id === selectedAgentId)) onSelectAgent("");
  }, [agents, selectedAgentId, onSelectAgent]);
  if (agents.length < 2) return null;
  return (
    <label className="composer-chip agent-chip">
      <Bot size={14} aria-hidden="true" />
      {/* The scope is part of the name: the pick is remembered on this thread
          (Conversation.default_agent_id), not on the session or the account,
          and a menu that names its scope is the Foyer trust rule. */}
      <select
        className="agent-select"
        value={selectedAgentId}
        onChange={(event) => onSelectAgent(event.target.value)}
        aria-label="Agent · this thread"
        title="Remembered on this thread"
      >
        <option value="">Default agent</option>
        {agents.map((agent) => (
          <option key={agent.id} value={agent.id}>
            {agent.name}
          </option>
        ))}
      </select>
    </label>
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
  thinking,
  setThinking,
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
          aria-label="Model · this thread"
          title="Remembered on this thread"
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
            aria-label="Reasoning effort · this thread"
            title="Remembered on this thread"
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
      {setThinking !== undefined && (
        <button
          type="button"
          className={thinking ? "composer-toggle on" : "composer-toggle"}
          onClick={() => setThinking(!thinking)}
          disabled={disabled}
          aria-pressed={thinking}
          title="Show the model's thinking trail while it works"
        >
          <Brain size={13} aria-hidden="true" /> Thinking
        </button>
      )}
    </>
  );
}

/**
 * The skills the composer's slash picker offers, fetched once when a surface
 * that has a picker mounts. Gated on `enabled` because ChatView is also mounted
 * beside a document, where there is no picker and so no reason to ask for the
 * list. Mirrors how AgentSelect fetches the agent list for itself.
 */
function useVisibleSkills(enabled: boolean): Skill[] {
  const [skills, setSkills] = useState<Skill[]>([]);
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    void api
      .listSkills()
      .then((rows) => {
        if (!cancelled) setSkills(rows);
      })
      .catch(() => undefined); // the composer works without a picker
    return () => {
      cancelled = true;
    };
  }, [enabled]);
  return skills;
}

/** Skills whose name/title/description contain the text typed after the "/". */
export function matchSkills(skills: Skill[], query: string): Skill[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return skills;
  return skills.filter((skill) =>
    `${skill.name} ${skill.title} ${skill.description}`.toLowerCase().includes(needle),
  );
}

/**
 * Whether an attached skill's required args are all filled, so the send button
 * can refuse a turn the server would only 422 anyway. A boolean arg is always
 * satisfied (its absence reads as false); everything else must be non-empty.
 */
export function argsSatisfied(skill: Skill | null, values: Record<string, unknown>): boolean {
  if (!skill) return true;
  return skill.args.every((arg) => {
    if (!arg.required || arg.type === "boolean") return true;
    const value = values[arg.name];
    return value !== undefined && value !== null && String(value).trim() !== "";
  });
}

/** The leading-"/" token stripped from a draft once a skill is attached. */
export function stripSlashToken(draft: string): string {
  return draft.replace(/^\/\S*\s?/, "");
}

/**
 * The autocomplete that opens when a composer draft starts with "/". It floats
 * above the composer rather than pushing it down, so the textarea does not jump
 * under the cursor mid-type.
 */
function SkillPicker({
  commands,
  commandMode,
  onPickCommand,
  skills,
  onPick,
}: {
  /** Built-in commands matching the query, listed above the skills with their
   *  own mark so a fixed verb of the product is distinguishable from something
   *  a teammate authored last week. */
  commands: BuiltinCommand[];
  /** The thread's approval mode, for the /plan toggle's two-way description. */
  commandMode: string | null;
  onPickCommand: (command: BuiltinCommand) => void;
  skills: Skill[];
  onPick: (skill: Skill) => void;
}) {
  return (
    <ul className="skill-picker" role="listbox" aria-label="Commands and skills">
      {commands.map((command) => (
        <li key={`command-${command.name}`}>
          <button
            type="button"
            onClick={() => onPickCommand(command)}
            role="option"
            aria-selected={false}
          >
            <span className="skill-picker-name">
              <Terminal size={13} aria-hidden /> /{command.name}
            </span>
            <span className="skill-picker-desc">
              {commandDescription(command, commandMode)}
            </span>
          </button>
        </li>
      ))}
      {skills.map((skill) => (
        <li key={skill.id}>
          <button type="button" onClick={() => onPick(skill)} role="option" aria-selected={false}>
            <span className="skill-picker-name">
              <Sparkles size={13} aria-hidden /> /{skill.name}
            </span>
            {skill.description && (
              <span className="skill-picker-desc">{skill.description}</span>
            )}
          </button>
        </li>
      ))}
    </ul>
  );
}

/**
 * The attached skill, shown as a chip on its own row above the composer tools so
 * it never crowds the agent/approval/model controls. Any args the skill declares
 * are prompted inline here — the smallest thing that lets a parameterised skill
 * be sent — and the whole row disappears the moment the skill is detached or the
 * send clears it.
 */
function SkillBar({
  skill,
  values,
  setArg,
  detach,
  disabled,
}: {
  skill: Skill;
  values: Record<string, unknown>;
  setArg: (name: string, value: unknown) => void;
  detach: () => void;
  disabled: boolean;
}) {
  return (
    <div className="skill-bar">
      <span className="skill-chip">
        <Sparkles size={13} aria-hidden />
        /{skill.name}
        <button
          type="button"
          onClick={detach}
          disabled={disabled}
          aria-label={`Remove skill ${skill.name}`}
        >
          <X size={12} />
        </button>
      </span>
      {skill.args.map((arg) => {
        const value = values[arg.name];
        const label = arg.label || arg.name;
        if (arg.type === "boolean") {
          return (
            <label key={arg.name} className="skill-arg-inline skill-arg-bool">
              <input
                type="checkbox"
                checked={Boolean(value)}
                disabled={disabled}
                onChange={(event) => setArg(arg.name, event.target.checked)}
              />
              {label}
            </label>
          );
        }
        if (arg.choices.length > 0) {
          return (
            <label key={arg.name} className="skill-arg-inline">
              <span>{label}</span>
              <select
                className="composer-select"
                value={value === undefined || value === null ? "" : String(value)}
                disabled={disabled}
                onChange={(event) => setArg(arg.name, event.target.value)}
                aria-label={label}
              >
                <option value="">—</option>
                {arg.choices.map((choice) => (
                  <option key={String(choice)} value={String(choice)}>
                    {String(choice)}
                  </option>
                ))}
              </select>
            </label>
          );
        }
        return (
          <label key={arg.name} className="skill-arg-inline">
            <span>{label}</span>
            <input
              className="skill-arg-input"
              type={arg.type === "string" ? "text" : "number"}
              value={value === undefined || value === null ? "" : String(value)}
              placeholder={arg.required ? "required" : "optional"}
              disabled={disabled}
              onChange={(event) => setArg(arg.name, event.target.value)}
              aria-label={label}
            />
          </label>
        );
      })}
    </div>
  );
}

/**
 * The composer's attach popover: pick a file, it uploads where you stand.
 *
 * Floats above the composer like the skill picker so the textarea does not
 * jump, closes itself once the upload settles, and says where the file went —
 * the one thing the old teleport communicated that staying put must not lose.
 */
function AttachMenu({
  attach,
  close,
}: {
  attach: NonNullable<ChatViewProps["attach"]>;
  close: () => void;
}) {
  const inputRef = { current: null as HTMLInputElement | null };
  // Two steps on purpose: the file is held here so its NAME can decide the
  // dataset offer before anything uploads. A CSV preselects "also make a
  // dataset" — the shape a chart question needs — and prose files never see
  // the checkbox at all.
  const [file, setFile] = useState<File | null>(null);
  const [makeDataset, setMakeDataset] = useState(false);
  const datasetOffered = Boolean(attach.createDataset) && file !== null && isTabular(file.name);

  async function add() {
    if (!file) return;
    const uploaded = await attach.upload([file]);
    if (uploaded && datasetOffered && makeDataset) {
      await attach.createDataset?.(baseName(uploaded.filename), uploaded.id);
    }
    close();
  }

  return (
    <div className="attach-menu" role="group" aria-label="Attach a file">
      <input
        ref={(node) => {
          inputRef.current = node;
        }}
        type="file"
        hidden
        onChange={(event) => {
          const picked = event.target.files?.[0] ?? null;
          setFile(picked);
          setMakeDataset(Boolean(picked && isTabular(picked.name)));
        }}
      />
      {file === null ? (
        <>
          <button
            type="button"
            className="primary-button"
            disabled={attach.uploading}
            onClick={() => inputRef.current?.click()}
          >
            <Paperclip size={14} />
            Choose a file
          </button>
          <p>
            Added to workspace knowledge, so this thread can cite it. Manage
            files under Knowledge › Sources.
          </p>
        </>
      ) : (
        <>
          <span className="attach-menu-file">{file.name}</span>
          {datasetOffered && (
            <label className="attach-menu-dataset">
              <input
                type="checkbox"
                checked={makeDataset}
                onChange={(event) => setMakeDataset(event.target.checked)}
              />
              Also create a dataset, so the agent can chart it
            </label>
          )}
          <button
            type="button"
            className="primary-button"
            disabled={attach.uploading}
            onClick={() => void add()}
          >
            <Paperclip size={14} />
            {attach.uploading ? "Uploading…" : "Add to workspace"}
          </button>
        </>
      )}
    </div>
  );
}

/**
 * What an empty conversation says the product is.
 *
 * A bare composer framed the product as retrieval-only — nothing taught the
 * verbs (attach, delegate, act-with-approval) that make it a workspace. Three
 * starter cards each prefill the composer with one of them, and the quiet line
 * underneath says the one thing a first-time user most needs to trust: nothing
 * happens to their stuff without their say-so.
 */
const STARTERS = [
  {
    title: "Summarize a file",
    detail: "Attach a document, then ask for the short version",
    draft: "Summarize the key points of the file I just attached.",
  },
  {
    title: "Build a chart from a CSV",
    detail: "Attach a spreadsheet and describe the chart",
    draft: "Build a chart from the CSV I attached: ",
  },
  {
    title: "Track a todo list",
    detail: "The agent keeps it, you tick it",
    draft: "Start a todo list called ",
  },
];

function ChatStarter({ setDraft }: { setDraft: (value: string) => void }) {
  return (
    <div className="chat-starter">
      <h2>Ask, and approve what the agent does</h2>
      <div className="chat-starter-cards">
        {STARTERS.map((starter) => (
          <button
            key={starter.title}
            type="button"
            onClick={() => setDraft(starter.draft)}
          >
            <strong>{starter.title}</strong>
            <span>{starter.detail}</span>
          </button>
        ))}
      </div>
      <p className="chat-starter-note">
        The agent asks before changing anything. You’ll approve its first action
        right here in the conversation.
      </p>
    </div>
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

/**
 * The prompt-injection screen's mark on a turn it flagged.
 *
 * Not a decoration and not tuned out like the clean citation case: it appears
 * only when the screen actually caught untrusted content — a retrieved passage,
 * the open document, or a tool result — trying to steer the assistant. In
 * enforce mode that turn was already forced to ask before every tool call; this
 * is what tells the reader an injection was the reason, so a parked write is not
 * read as the assistant being needlessly cautious. `role="alert"` because a
 * caught injection is exactly the event a screen reader should hear.
 */
function ScreenFlagNote() {
  return (
    <div className="screen-flag" role="alert">
      <ShieldAlert size={14} aria-hidden="true" />
      <div>
        <strong>Prompt injection screened</strong>
        <span>
          Untrusted content in this turn tried to steer the assistant. Every tool
          call was held for your approval.
        </span>
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

/**
 * The mid-turn steering strip: a one-line note into the run as it works.
 *
 * Its own component so the draft's state mounts and unmounts with the strip —
 * the same rule the attach popover follows: state that only makes sense while
 * the surface is visible lives inside it, and the unmount is the reset.
 */
function SteerStrip({ steer }: { steer: (content: string) => Promise<boolean> }) {
  const [note, setNote] = useState("");
  const [sending, setSending] = useState(false);

  async function send() {
    const content = note.trim();
    if (!content || sending) return;
    setSending(true);
    try {
      // Only a delivered note clears the box: a 409 (the run parked or
      // finished in the race) or a network failure keeps the user's sentence
      // where they can resend or copy it into the composer.
      if (await steer(content)) setNote("");
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="steer-strip">
      <input
        type="text"
        aria-label="Add a note to the running turn"
        placeholder="Add a note mid-task — it reaches the assistant before its next step"
        value={note}
        onChange={(event) => setNote(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            void send();
          }
        }}
      />
      <button
        type="button"
        className="ghost-button"
        disabled={sending || !note.trim()}
        onClick={() => void send()}
      >
        Steer
      </button>
    </div>
  );
}

function ToolCallCard({
  call,
  decide,
  todos,
  pinning,
}: {
  call: AgentToolCall;
  decide: ToolDecision;
  todos?: { lists: Board[]; ops: TodoOps };
  pinning?: DashboardPinning;
}) {
  const [expanded, setExpanded] = useState(false);
  const [remember, setRemember] = useState(false);
  const [busy, setBusy] = useState(false);
  // The ask_user card's answer box. Local to the card like `expanded`: the
  // draft belongs to this one question and dies with it.
  const [answer, setAnswer] = useState("");
  const asking = call.name === "ask_user";
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
      const typed = answer.trim();
      await decide(
        call,
        decision,
        remember,
        asking && decision === "approved" && typed ? { answer: typed } : undefined,
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    // data-call-id is the waiting banner's jump target — the strip above the
    // composer scrolls the transcript back to the card that is asking.
    <div className={`tool-card ${call.status}`} data-call-id={call.id}>
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
      {/* The plan-review card's preview IS the plan, written as markdown for a
          person to read — a diff renderer would strip its structure. */}
      {preview &&
        (call.name === "exit_plan_mode" ? (
          <div className="plan-proposal">
            <MarkdownBody content={preview} />
          </div>
        ) : (
          <ProposalDiff preview={preview} />
        ))}
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
      {/* Above the figure, so the offer is read before the scroll past it.
          Renders nothing for calls that are not chart-shaped, and nothing at
          all on the panels that mount ChatView without a `pinning` bundle. */}
      <DashboardPinBar call={call} pinning={pinning} />
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
          {/* No "always allow" on the plan-review card: the server never
              consults a standing grant for it (approving the card IS approving
              this plan), so the checkbox would promise a skip that cannot
              happen. Same for ask_user, which parks by construction — a
              standing allow could never pre-answer a question to a person. */}
          {call.name !== "exit_plan_mode" && !asking && (
            <label className="remember">
              <input
                type="checkbox"
                checked={remember}
                onChange={(event) => setRemember(event.target.checked)}
              />
              {/* "For me", because that is the grant's true width: it writes a
                  caller-personal, chat-scope rule — never the workspace's. */}
              Always allow {call.name} for me
              <span className="field-hint">Manage in Inbox → Rules</span>
            </label>
          )}
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
              <Check size={14} /> {asking && answer.trim() ? "Answer" : "Approve"}
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
  flaggedRuns,
  sharedThread,
  submitPrompt,
  cancelActiveRun,
  steer,
  regenerate,
  editMessage,
  viewerId,
  decideAgentCall,
  openCitation,
  attach,
  approval,
  showStarter,
  unrestricted,
  todos,
  pinning,
  endRef,
  selectedAgentId,
  onSelectAgent,
  turnControls,
  skills,
  thinking,
  fork,
  undo,
}: ChatViewProps) {
  // Tool calls belong to a run, and every message carries its run_id, so they
  // stay anchored to the right turn after a reload rather than only while live.
  const callsForRun = (runId: string) =>
    agentCalls.filter((call) => call.run_id === runId);
  // The slash picker: open only when a picker exists, nothing is attached yet,
  // and the draft leads with "/". The matches drive both the dropdown and the
  // Enter-to-attach shortcut, so they are computed once here.
  const skillList = useVisibleSkills(Boolean(skills));
  // The attach popover, open or not. Local state like `expanded` on a tool
  // card: nothing outside the composer cares, and closing must not re-render
  // the transcript.
  const [attachOpen, setAttachOpen] = useState(false);
  // Which message is an editor right now, and what it says. View state (not
  // row state) so exactly one edit can be open at a time — the rail's rename
  // pattern. Deliberately no on-blur submit anywhere below: an edit deletes
  // everything after the message, and a destructive act must never ride a
  // stray click.
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState("");
  const submitEdit = async (messageId: string) => {
    if (!editMessage || !editDraft.trim()) return;
    const accepted = await editMessage(messageId, editDraft.trim());
    // Success closes the editor; failure keeps the rewritten words on screen.
    if (accepted) setEditingId(null);
  };
  const slashQuery =
    skills && !skills.attached && draft.startsWith("/") ? draft.slice(1) : null;
  const skillMatches = slashQuery === null ? [] : matchSkills(skillList, slashQuery);
  // Built-in commands share the picker. /plan needs the approval control (a
  // subject panel has none, and a mode it cannot show must not be settable
  // from it); /btw works wherever the composer does.
  const commandMatches = (slashQuery === null ? [] : matchCommands(slashQuery)).filter(
    (command) => command.name !== "plan" || Boolean(approval),
  );
  const pickerOpen =
    slashQuery !== null && skillMatches.length + commandMatches.length > 0;
  const attachSkill = (skill: Skill) => {
    skills?.attach(skill);
    setDraft(stripSlashToken(draft));
  };
  const pickCommand = (command: BuiltinCommand) => {
    if (command.name === "plan" && approval) {
      // A toggle, resolved immediately — nothing rides the next send. Leaving
      // plan mode by hand restores the default, the same landing the approved
      // exit uses: re-arming a bypass nobody re-asked for is the surprise the
      // modes exist to avoid.
      approval.setMode(approval.mode === "plan" ? "ask_writes" : "plan");
      setDraft(stripSlashToken(draft));
      return;
    }
    // /btw: complete the token and let the note be typed after it; the send
    // path recognises the finished draft and records it as an aside.
    setDraft(`/btw ${stripSlashToken(draft)}`);
  };
  // A turn cannot be sent with a required arg left blank; the button says so
  // rather than letting the server 422 a click the composer could have refused.
  const skillReady = argsSatisfied(skills?.attached ?? null, skills?.argValues ?? {});
  // Either kind of "writes are going through unreviewed". The composer zone
  // wears the warning treatment for both, because from the reader's side they
  // are the same fact — and the development one is the wider of the two.
  const bypassed = Boolean(approval && isBypass(approval.mode)) || Boolean(unrestricted);
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
        {/* Only the primary chat teaches; the panels beside a document or
            dashboard are scoped to a subject and have no `approval` prop, so
            they keep their quiet empty state. */}
        {messages.length === 0 && approval && showStarter !== false && (
          <ChatStarter setDraft={setDraft} />
        )}
        {messages.length > 0 && (
          <div className="message-column">
            {messages.map((message) => {
              // An aside ("/btw") is a user message with no run — a note the
              // agent will read later, not a prompt it answered — so it wears
              // a quieter treatment than a turn, and it never grows a pencil:
              // there is no turn after it to re-run.
              const aside = message.role === "user" && message.run_id === "";
              const editable =
                Boolean(editMessage) &&
                !aside &&
                senderIsViewer(message, Boolean(sharedThread), viewerId ?? "");
              return (
              <article
                key={message.id}
                className={`message ${message.role}${aside ? " aside" : ""}`}
              >
                {message.role === "assistant" &&
                  (() => {
                    const calls = callsForRun(message.run_id);
                    const showChecklist = checklistCallId(calls);
                    // The undo affordance rides the turn's tool-card group: it
                    // exists only where a finished run actually executed a
                    // call, never on the run still streaming. The handler owns
                    // the confirm and the skipped-effects summary.
                    const undoable =
                      undo &&
                      message.run_id !== activeRun &&
                      calls.some((call) => call.status === "succeeded");
                    return (
                      <>
                        {calls.map((call) => (
                          <ToolCallCard
                            key={call.id}
                            call={call}
                            decide={decideAgentCall}
                            todos={call.id === showChecklist ? todos : undefined}
                            pinning={pinning}
                          />
                        ))}
                        {undoable && (
                          <button
                            type="button"
                            className="fork-button undo-run-button"
                            aria-label="Undo this run's changes"
                            title="Undo this run's changes"
                            onClick={() => void undo(message.run_id)}
                          >
                            <Undo2 size={13} />
                            <span>Undo this run&rsquo;s changes</span>
                          </button>
                        )}
                      </>
                    );
                  })()}
                <div className="message-author">
                  {message.role === "user" ? (
                    <div className="tiny-avatar">
                      {senderInitial(message, Boolean(sharedThread))}
                    </div>
                  ) : (
                    <div className="assistant-mark">A</div>
                  )}
                  {/* On a shared thread the sender's name says who spoke — a
                      teammate's turn is not "You". On a personal thread every
                      user message is the caller's, so the name would be noise. */}
                  <span>{senderLabel(message, Boolean(sharedThread))}</span>
                  {message.role === "assistant" && message.content && (
                    <CopyButton value={message.content} label="Copy message" />
                  )}
                  {/* Disabled rather than hidden while a run streams: the
                      server would 409 an edit over a live turn, and a control
                      that vanishes and reappears reads as a bug. The name
                      quotes the prompt so each row's pencil is distinct to a
                      screen reader. */}
                  {editable && (
                    <button
                      type="button"
                      className="copy-button"
                      aria-label={`Edit: ${message.content.slice(0, 40)}`}
                      disabled={Boolean(activeRun)}
                      onClick={() => {
                        setEditingId(message.id);
                        setEditDraft(message.content);
                      }}
                    >
                      <Pencil size={13} /> Edit
                    </button>
                  )}
                  {/* Branch a fresh thread from everything said up to here.
                      Any message is a fork point — the reply you want to
                      re-ask after, or your own question worth re-asking — and
                      the server copies the prefix, so this is one call and a
                      jump, not a client-side splice. */}
                  {fork && (
                    <button
                      type="button"
                      className="fork-button"
                      aria-label="Fork thread from this message"
                      title="Fork thread from this message"
                      onClick={() => void fork(message.id)}
                    >
                      <GitFork size={13} />
                    </button>
                  )}
                </div>
                <div className="message-body">
                  {message.role === "assistant" ? (
                    <MarkdownBody content={message.content} />
                  ) : editingId === message.id ? (
                    <div className="message-edit">
                      <textarea
                        value={editDraft}
                        onChange={(event) => setEditDraft(event.target.value)}
                        aria-label="Edit message"
                        rows={3}
                        autoFocus
                        onKeyDown={(event) => {
                          if (event.key === "Enter" && !event.shiftKey) {
                            event.preventDefault();
                            void submitEdit(message.id);
                          }
                          if (event.key === "Escape") {
                            event.preventDefault();
                            setEditingId(null);
                          }
                        }}
                      />
                      <div className="message-edit-actions">
                        {/* Save is the truncation: the old turn and everything
                            after it go, and the fresh turn streams in. */}
                        <button
                          type="button"
                          className="ghost-button"
                          onClick={() => setEditingId(null)}
                        >
                          Cancel
                        </button>
                        <button
                          type="button"
                          className="primary-button"
                          disabled={!editDraft.trim() || Boolean(activeRun)}
                          onClick={() => void submitEdit(message.id)}
                        >
                          Save
                        </button>
                      </div>
                    </div>
                  ) : (
                    /* User-authored content is plain text on purpose, and
                       this branch is a security boundary: inbound email
                       lands as user-role messages (run_id ""), so rendering
                       markdown here would let a hostile mail auto-load
                       remote images (tracking pixels) or dress a phishing
                       URL in friendly link text. services/inbound_email.py's
                       strip_html contract leans on exactly this — change the
                       two together or not at all.

                       The clause this comment used to be missing: plain text
                       is not the whole story, because ChatDashboardEmbeds
                       below reads the same string for every role. See its
                       comment for what a hostile mail can and cannot reach
                       through it. */
                    <p>{message.content}</p>
                  )}
                </div>
                {/* Unconditionally rendered, and that is the point: it returns
                    null when the message names no app, so its position in this
                    subtree never changes. A conditional here would take the
                    iframe out of the tree and put it back — which reloads the
                    frame — the first time a streamed message crossed the line
                    between naming an app and not.

                    Unconditional across *roles* too, which is worth stating
                    plainly next to the plain-text boundary above: a user-role
                    message can mount an embed, and inbound email lands as
                    user-role. What that reaches is bounded by the component,
                    not by the sender — only a slug this workspace already
                    lists, already published public, already holding a release
                    embeds at all; the frame is ADR 0004's (opaque origin,
                    sandbox="allow-scripts", no network); and no `api`/
                    `bindings` are passed, so it renders the release's frozen
                    snapshot and cannot query. A hostile mail can therefore
                    put a dashboard the workspace already publishes into the
                    transcript — noise, next to a message the reader can see
                    is an email — and nothing else. Widening any of those
                    three (private apps, live bindings, a relaxed frame) turns
                    that noise into a real finding. */}
                <ChatDashboardEmbeds content={message.content} apps={apps} />
                {message.role === "assistant" &&
                  flaggedRuns?.includes(message.run_id) && <ScreenFlagNote />}
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
              );
            })}
            {liveCalls.map((call) => (
              <ToolCallCard
                key={call.id}
                call={call}
                decide={decideAgentCall}
                todos={call.id === checklistCallId(liveCalls) ? todos : undefined}
                pinning={pinning}
              />
            ))}
            {/* The flagged turn's mark while it is still live: a run that parked
                on the injection escalation has no assistant message yet, so the
                per-message mark above cannot appear until it settles. Shown only
                when this run has no message to carry it, to avoid a duplicate. */}
            {activeRun &&
              flaggedRuns?.includes(activeRun) &&
              !messages.some(
                (message) =>
                  message.role === "assistant" && message.run_id === activeRun,
              ) && <ScreenFlagNote />}
            {/* Sits where a tool card would, and is deliberately not one: the
                run parked before it asked the model anything, so there is no
                proposed call and an approve/deny pair would decide nothing. */}
            {activeRun && budgetPark && (
              <BudgetHold park={budgetPark} menuId="chat-spend-ceiling" />
            )}
            {/* The thinking trail: live narration in its own quiet lane above
                the status line, open by default because the person turned it
                on to watch. Ephemeral — it clears when the run settles. */}
            {activeRun && thinking && (
              <details className="agent-provisioning thinking-trail" open>
                <summary className="mcp-card-meta">Thinking</summary>
                <p
                  className="agent-instructions"
                  style={{ whiteSpace: "pre-wrap" }}
                >
                  {thinking}
                </p>
              </details>
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
            {steer &&
              steerStripVisible({
                activeRun,
                hasSteer: true,
                budgetPark,
                runStatus,
                agentCalls,
              }) && <SteerStrip steer={steer} />}
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
        {/* Blocked-on-you, in the non-scrolling zone. "Working…" and "waiting
            for your approval" are opposite states — one needs patience, the
            other needs a decision — and a card in a transcript can be scrolled
            past. This strip cannot, and it stays until the decision is made. */}
        {activeRun &&
          (() => {
            const parked = agentCalls.find(
              (call) => call.run_id === activeRun && call.status === "proposed",
            );
            if (!parked) return null;
            return (
              <div className="waiting-banner" role="status">
                <span>
                  ⏸ Waiting for your approval — <strong>{parked.name}</strong>
                </span>
                <button
                  type="button"
                  className="ghost-button"
                  onClick={() =>
                    document
                      .querySelector(`[data-call-id="${parked.id}"]`)
                      ?.scrollIntoView({ behavior: "smooth", block: "center" })
                  }
                >
                  Jump to request
                </button>
              </div>
            );
          })()}
        {/* In the composer zone rather than in the transcript, and that is the
            whole design: a transcript scrolls, so a warning placed in one is a
            warning you can leave behind above the fold. This cannot be
            scrolled away from while the bypass is on. */}
        {/* The development bypass outranks the per-thread one in the banner:
            it is wider (every thread, every tool) and it cannot be turned off
            from here, so showing the thread-level "Turn off" button beside it
            would offer a fix that does not fix this. */}
        {unrestricted ? (
          <UnrestrictedIndicator
            approved={autoApprovedCalls(agentCalls, unrestricted.conversationId)}
          />
        ) : (
          approval &&
          bypassed && (
            <BypassIndicator
              conversationTitle={approval.conversationTitle}
              approved={autoApprovedCalls(agentCalls, approval.conversationId)}
              stop={() => approval.setMode("ask_writes")}
            />
          )
        )}
        <div className="composer-shell">
          {pickerOpen && (
            <SkillPicker
              commands={commandMatches}
              commandMode={approval?.mode ?? null}
              onPickCommand={pickCommand}
              skills={skillMatches}
              onPick={attachSkill}
            />
          )}
          {attach && attachOpen && (
            <AttachMenu attach={attach} close={() => setAttachOpen(false)} />
          )}
          <form className="composer" onSubmit={(event) => void submitPrompt(event)}>
          <textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                // With the slash picker open, Enter picks the top match —
                // command first, then skill — rather than sending a message
                // that is only a "/query".
                if (pickerOpen) {
                  if (commandMatches.length > 0) pickCommand(commandMatches[0]);
                  else attachSkill(skillMatches[0]);
                  return;
                }
                void submitPrompt();
              }
            }}
            // The hint is the empty workspace's only instruction, so it stays
            // visible; the accessible name is stable ("Message") so tests and
            // screen readers do not depend on whether a source is indexed yet.
            placeholder={
              activeRun
                ? "Steer the assistant — your note joins this turn…"
                : sources.some((source) => source.status === "ready")
                  ? "Ask your workspace…"
                  : "Upload a source, then ask a question…"
            }
            rows={1}
            aria-label="Message"
          />
          {skills?.attached && (
            <SkillBar
              skill={skills.attached}
              values={skills.argValues}
              setArg={skills.setArg}
              detach={skills.detach}
              disabled={Boolean(activeRun)}
            />
          )}
          <div className="composer-tools">
            {/* The three entry points that used to be invisible — a bare
                paperclip, a dropdown that hid below two agents, and a "/"
                incantation nothing advertised — are labelled chips now. The
                composer is the product's front door; its verbs say their names. */}
            {attach && (
              <button
                type="button"
                className={attachOpen ? "composer-chip on" : "composer-chip"}
                onClick={() => setAttachOpen((value) => !value)}
                aria-expanded={attachOpen}
                aria-label="Attach a file"
              >
                <Paperclip size={14} />
                Attach
              </button>
            )}
            {onSelectAgent && (
              <AgentSelect
                selectedAgentId={selectedAgentId ?? ""}
                onSelectAgent={onSelectAgent}
              />
            )}
            {skills && !skills.attached && (
              <button
                type="button"
                className="composer-chip"
                // "/" in an empty draft opens the picker the same way typing it
                // does — the chip is the discoverable name for the incantation,
                // not a second mechanism.
                onClick={() => {
                  if (!draft.startsWith("/")) setDraft(`/${draft}`);
                }}
                disabled={Boolean(activeRun)}
                aria-label="Use a skill"
              >
                <Sparkles size={14} />
                Skills
              </button>
            )}
            {turnControls && (
              <TurnControls {...turnControls} disabled={Boolean(activeRun)} />
            )}
            {approval && (
              <ApprovalModeControl mode={approval.mode} setMode={approval.setMode} />
            )}
            <span className="composer-spacer" />
            {activeRun ? (
              <>
                {/* Steering: the same box stays live during a run, and a
                    non-empty draft sends INTO the running turn. Stop keeps
                    its place beside it — typing must never hide the brake. */}
                {draft.trim() !== "" && (
                  <button
                    className="send-button"
                    type="submit"
                    aria-label="Steer the run"
                  >
                    <ArrowUp size={18} />
                  </button>
                )}
                <button
                  type="button"
                  className="send-button stop"
                  onClick={() => void cancelActiveRun()}
                  aria-label="Stop generating"
                >
                  <Square size={15} />
                </button>
              </>
            ) : (
              <button
                className="send-button"
                type="submit"
                disabled={!draft.trim() || !skillReady}
                aria-label="Send message"
              >
                <ArrowUp size={18} />
              </button>
            )}
          </div>
          </form>
        </div>
      </div>
    </section>
  );
}
