"use client";

import {
  ArrowUp,
  Ban,
  Bot,
  Brain,
  Check,
  ChevronRight,
  Copy,
  EyeOff,
  FileText,
  GitFork,
  Mic,
  Paperclip,
  Pencil,
  Plus,
  RefreshCw,
  ShieldAlert,
  Sparkles,
  Square,
  Terminal,
  ThumbsDown,
  ThumbsUp,
  ListChecks,
  Undo2,
  Wrench,
  X,
  Zap,
} from "lucide-react";
import type {
  ChatAttachment,
  AgentInfo,
  AgentToolCall,
  ApprovalMode,
  Board,
  Citation,
  CoworkingPresence,
  GeneratedApp,
  Message,
  RunPreset,
  Skill,
  Source,
  StylePreset,
} from "@workspace/api-client";
import {
  type CSSProperties,
  FormEvent,
  type SetStateAction,
  useEffect,
  useRef,
  useState,
} from "react";
import { api } from "../api";
import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkMath from "remark-math";
import { useVirtualizer } from "@tanstack/react-virtual";
import { ChatDashboardEmbeds } from "../chat-dashboard-embed";
import { CsvPeek, ImageThumb, PdfCard } from "../attachment-previews";
import { LiveCursorLayer } from "../live-cursors";
import { ArtifactImages } from "../source-image";
import { previewKindOf } from "./attachment-preview";
import {
  applyResult,
  beginDictation,
  composedDraft,
  resultWrite,
  supportsDictation,
  type DictationResult,
  type DictationState,
} from "./dictation";
import type { CoworkingState } from "../use-coworking";
import { autoApprovedCalls, describeGate, isBypass } from "./approval-format";
import { CoverageDrawer } from "./coverage-drawer";
import { describeFollowup, orderFollowups } from "./followup-format";
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
import { CitationVerdictNote, GroundingDrawer } from "./citation-note";
import type { RunPlan } from "./plan-format";
import { ProposalDiff } from "./proposal-diff";
import { DashboardPinBar, type DashboardPinning } from "./dashboard-pin-bar";
import {
  baseName,
  isStreamingMessage,
  isTabular,
  senderInitial,
  senderIsViewer,
  senderLabel,
  type View,
} from "./shared";
import { TODO_TOOLS, listForTodoCall } from "./todo-format";
import { TodoChecklist, type TodoOps } from "./todos";

/**
 * Who is typing into this thread right now, besides the viewer. Fed by the
 * presence heartbeat use-workspace.ts already sends on `conversation:<id>`
 * (state `{typing}` only — never a draft); this is purely the receiving end.
 */
export function typersOn(
  presences: CoworkingPresence[],
  surface: string,
  viewerId: string,
): CoworkingPresence[] {
  return presences.filter(
    (presence) =>
      presence.surface === surface &&
      presence.actor_id !== viewerId &&
      presence.state.typing === true,
  );
}

/** The typing line's sentence; "" when nobody is. */
export function typingLine(names: string[]): string {
  if (names.length === 0) return "";
  if (names.length === 1) return `${names[0]} is typing…`;
  if (names.length === 2) return `${names[0]} and ${names[1]} are typing…`;
  return "Several people are typing…";
}

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
  /** Accepts the functional form — every mount passes a real state setter,
   *  and the mic button's race guard depends on it (see MicButton). */
  setDraft: (value: SetStateAction<string>) => void;
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
   * Put a follow-up chip's question into THIS pane's composer. It SEEDS and
   * never sends — a chip that sent would let a stray click spend a turn — and
   * it appends to a draft already in flight rather than clobbering it. Absent
   * on the panels that render no chips.
   */
  seedDraft?: (text: string) => void;
  /**
   * Offer the coverage drawer under each answer. Off for the panels beside a
   * document or a dashboard, whose turns are not research runs and would show
   * an empty drawer on every message.
   */
  showCoverage?: boolean;
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
    /**
     * Attach the file to THIS THREAD instead of to the workspace.
     *
     * The two destinations are a real choice and not a preference. A library
     * upload is a claim about what the workspace knows and is retrievable from
     * every thread forever; an attachment is a claim about what this
     * conversation is about, is retrievable only here, and — when the file is
     * text — comes back as a document this pane can open and edit. Most files
     * dropped into a chat are meant for the chat, so this is the default the
     * popover leads with.
     */
    attachToChat?: (files: FileList | File[]) => Promise<ChatAttachment | null>;
    attaching?: boolean;
    /** What is already attached to this thread. */
    attachments?: ChatAttachment[];
    detach?: (attachment: ChatAttachment) => Promise<void>;
    /** Open an attached document for editing, as a column beside the chat. */
    openFile?: (documentId: string, filename: string) => void;
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
    /**
     * The member has Safe mode on — their threads are seeded to ask first.
     *
     * Here so the auto-approve banner can tell a *departure* from a *default*.
     * Writes going through unreviewed is the same fact either way and the trail
     * is shown either way; what changes is whether it is news. For a member who
     * asked to be asked, a thread running unreviewed is the thing they need
     * shouted at them; for everyone else it is how the product works, and a
     * standing alarm on every thread is an alarm nobody reads by Tuesday.
     */
    safeMode: boolean;
  };
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
  todos?: { lists: Board[]; ops: TodoOps; selfId?: string };
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
    /**
     * The research-preset picker: the catalogue from bootstrap, the pick, and
     * the setter that SEEDS the other controls from it. Optional like the
     * Thinking toggle — a surface with no catalogue shows no picker.
     */
    presets?: RunPreset[];
    preset?: string;
    setPreset?: (value: string) => void;
    /** Plan-then-execute for the next turn. A per-turn toggle, like Fast. */
    stepPlan?: boolean;
    setStepPlan?: (value: boolean) => void;
  };
  /** The live thinking trail streamed by the active run; "" between runs. */
  thinking?: string;
  /**
   * The live plan a step-plan turn is working through; null between runs and
   * for every turn that is not planning. Narration, not transcript — see
   * `views/plan-format`.
   */
  plan?: RunPlan;
  /**
   * The member's persistent response style, for the composer's picker. The
   * "· you" scope suffix mirrors effort's "· this thread": this one follows
   * the MEMBER across every thread, and a picker that did not say so would
   * read as per-thread state. `onChange` is the same persistent handler the
   * settings menu uses. Optional like `turnControls`: the side-panel mounts
   * omit it and render no picker.
   */
  responseStyle?: {
    preset: string;
    customText: string;
    onChange: (preset: StylePreset, customText: string) => void;
  };
  /**
   * Thumbs up/down on an assistant message. Optional like `fork`: only the
   * mounts that wire it show the icons, so side-panel mounts stand unchanged.
   * The caller owns the POST and the optimistic `my_feedback` patch.
   */
  feedback?: (
    messageId: string,
    verdict: "up" | "down",
    note: string,
  ) => Promise<void>;
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
  /**
   * Jump to another view from the composer's "+" menu — the point-of-use doors
   * to Sources, Datasets and the Gallery. Only the shell owns setView, so only
   * the primary rail chat passes it; the document-side and split-pane mounts
   * omit it and the menu simply shows no navigation rows.
   */
  openView?: (view: View) => void;
  /**
   * Hand the current draft to the Schedules composer — the "+" menu's
   * "Do this on a schedule…" row. Only the shell owns view navigation, so
   * only the primary rail chat passes it; the side-panel mounts show no such
   * row. The draft is deliberately NOT cleared from the composer — navigating
   * to Schedules must not eat the words.
   */
  scheduleDraft?: (draft: string) => void;
  /**
   * The thread's temporary-chat state: its runs neither recall nor store
   * memories. `toggle` is present only while it can still do anything — the
   * server stamps incognito at creation, so the pre-thread composer offers
   * the flip and an existing thread only reports the fact. Optional like
   * `approval`: only the primary rail chat passes it.
   */
  incognito?: { on: boolean; toggle?: () => void };
  /**
   * The shell's live-coworking channel, with the id of the conversation this
   * view is showing. Both matter only on a SHARED thread — pointer cursors
   * over the transcript and the typing line above the composer — and the
   * mounts are gated on `sharedThread` so a personal thread never even emits
   * a pointer beat (the server's visibility gate would hide it anyway; this
   * is belt and braces plus zero wasted heartbeats).
   */
  coworking?: CoworkingState;
  conversationId?: string;
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

/** "xhigh" → "Extra high"; every other effort just gets its first letter cased. */
function effortLabel(effort: string): string {
  if (effort === "xhigh") return "Extra high";
  return effort.charAt(0).toUpperCase() + effort.slice(1);
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
  presets,
  preset,
  setPreset,
  stepPlan,
  setStepPlan,
  disabled,
}: NonNullable<ChatViewProps["turnControls"]> & { disabled: boolean }) {
  return (
    <>
      {/* First, because it SEEDS the controls after it: a user reads left to
          right and a picker that silently rewrote the dropdown to its left
          would look like a bug rather than a policy. */}
      {presets && presets.length > 0 && setPreset && (
        <select
          className="composer-select"
          value={preset || ""}
          onChange={(event) => setPreset(event.target.value)}
          disabled={disabled}
          aria-label="Run preset · this thread"
          title={
            presets.find((row) => row.name === preset)?.description ||
            "Pick how thoroughly this thread answers"
          }
        >
          <option value="">No preset</option>
          {presets.map((row) => (
            <option key={row.name} value={row.name} title={row.description}>
              {row.label}
            </option>
          ))}
        </select>
      )}
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
                {effortLabel(name)}
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
      {setStepPlan !== undefined && (
        <button
          type="button"
          className={stepPlan ? "composer-toggle on" : "composer-toggle"}
          onClick={() => setStepPlan(!stepPlan)}
          disabled={disabled}
          aria-pressed={Boolean(stepPlan)}
          title="Plan: break the question into steps before answering"
        >
          <ListChecks size={13} aria-hidden="true" /> Plan
        </button>
      )}
    </>
  );
}

/**
 * The plan a step-plan turn committed to, filling in as it works.
 *
 * Rendered in the same slot as the thinking trail and for the same reason: it
 * is narration for the person watching, not part of the transcript. `aria-live`
 * is polite rather than assertive — a step finishing is worth announcing, and
 * worth announcing without interrupting whatever the reader is on.
 */
function PlanTrail({ plan }: { plan: RunPlan }) {
  if (!plan || !plan.steps.length) return null;
  return (
    <ol className="plan-trail" aria-live="polite" aria-label="Plan progress">
      {plan.steps.map((step) => (
        <li
          key={step.index}
          className={step.done ? "plan-trail-step done" : "plan-trail-step"}
        >
          <span className="plan-trail-question">
            {step.done ? <Check size={12} aria-hidden="true" /> : null}
            {step.question}
          </span>
          {step.queries.length > 0 && (
            <span className="plan-trail-queries">
              {step.queries.map((query) => (
                <span className="plan-trail-query" key={query}>
                  {query}
                </span>
              ))}
            </span>
          )}
          {step.summary && (
            <span className="plan-trail-summary">{step.summary}</span>
          )}
        </li>
      ))}
    </ol>
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
 * The composer's attach popover: pick a file, then say where it belongs.
 *
 * Floats above the composer like the skill picker so the textarea does not
 * jump, closes itself once the upload settles, and says where the file went —
 * the one thing the old teleport communicated that staying put must not lose.
 *
 * Two destinations, because they are genuinely different promises and the
 * difference is invisible after the fact. "This chat" scopes the file to the
 * thread and, for text, makes it editable; "workspace knowledge" publishes it
 * to every thread there will ever be. Leading with the chat is not a guess
 * about which is more common so much as which is more recoverable: a file
 * attached to one thread can be promoted later, while a file already indexed
 * into the library has already been read by every other conversation.
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

  const busy = attach.uploading || Boolean(attach.attaching);

  async function add() {
    if (!file) return;
    const uploaded = await attach.upload([file]);
    if (uploaded && datasetOffered && makeDataset) {
      await attach.createDataset?.(baseName(uploaded.filename), uploaded.id);
    }
    close();
  }

  async function addToChat() {
    if (!file) return;
    await attach.attachToChat?.([file]);
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
            {attach.attachToChat
              ? "Attach it to this chat, or add it to workspace knowledge."
              : "Added to workspace knowledge, citable from this thread."}
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
              Also create a dataset
            </label>
          )}
          {attach.attachToChat && (
            <>
              <button
                type="button"
                className="primary-button"
                disabled={busy}
                onClick={() => void addToChat()}
              >
                <Paperclip size={14} />
                {attach.attaching ? "Attaching…" : "Attach to this chat"}
              </button>
              <p className="attach-menu-note">
                Only this thread sees it. Text files open here for editing.
              </p>
            </>
          )}
          <button
            type="button"
            className={attach.attachToChat ? "ghost-button" : "primary-button"}
            disabled={busy}
            onClick={() => void add()}
          >
            <Paperclip size={14} />
            {attach.uploading ? "Uploading…" : "Add to workspace"}
          </button>
          {attach.attachToChat && (
            <p className="attach-menu-note">
              Every thread can cite it, now and later.
            </p>
          )}
        </>
      )}
    </div>
  );
}

/**
 * The composer's "+" menu: the discoverable index of everything the composer
 * can do, at the point of use. Floats above the composer like AttachMenu and
 * closes on pick. Every row reuses an existing handler — this menu is
 * exposure, not capability: the Attach and Skills chips stay beside it as the
 * fast path, and the navigation rows only render when the shell wired
 * `openView` (the side-panel mounts have no setView to give).
 */
function PlusMenu({
  rows,
  close,
}: {
  rows: { label: string; run: () => void; disabled?: boolean; title?: string }[];
  close: () => void;
}) {
  return (
    <div className="attach-menu plus-menu" role="group" aria-label="Tools">
      {rows.map((row) => (
        <button
          key={row.label}
          type="button"
          className="ghost-button plus-menu-row"
          disabled={row.disabled}
          title={row.title}
          onClick={() => {
            row.run();
            close();
          }}
        >
          {row.label}
        </button>
      ))}
    </div>
  );
}

/** The slice of the Web Speech API the mic adapter touches — typed
 *  structurally because lib.dom has no SpeechRecognition declarations. */
type RecognitionEventLike = {
  resultIndex: number;
  results: ArrayLike<{ isFinal: boolean; 0: { transcript: string } }>;
};
type RecognitionLike = {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  onresult: ((event: RecognitionEventLike) => void) | null;
  onerror: (() => void) | null;
  onend: (() => void) | null;
  start: () => void;
  stop: () => void;
};

/**
 * Voice dictation: a thin adapter over the pure reducer in dictation.ts,
 * which is where the behaviour is tested — the API itself cannot be
 * exercised in CI (headless Chrome exposes the constructor and then fails
 * recognition with a network error at runtime).
 *
 * Rendered only after a MOUNTED feature detect, never at SSR, so the server
 * and first client render agree. Recording stops on: the button, a manual
 * draft edit (which includes send clearing the draft), recognition's own
 * error or end (no toast — see above), and unmount. Everything is
 * browser-local; no audio or transcript leaves the page.
 */
function MicButton({
  draft,
  setDraft,
}: {
  draft: string;
  /** SetStateAction-capable: onresult's manual-edit guard needs the
   *  functional form to see a keystroke's still-queued write. */
  setDraft: (value: SetStateAction<string>) => void;
}) {
  const [supported, setSupported] = useState(false);
  const [recording, setRecording] = useState(false);
  const recognitionRef = useRef<RecognitionLike | null>(null);
  const stateRef = useRef<DictationState | null>(null);
  // The last draft THIS button wrote; anything else on the wire is a manual
  // edit and ends the session.
  const composedRef = useRef("");

  useEffect(() => {
    setSupported(supportsDictation(typeof window === "undefined" ? null : window));
  }, []);

  const stop = () => {
    const recognition = recognitionRef.current;
    recognitionRef.current = null;
    stateRef.current = null;
    setRecording(false);
    if (recognition) {
      recognition.onresult = null;
      recognition.onerror = null;
      recognition.onend = null;
      try {
        recognition.stop();
      } catch {
        // Already stopped; idle is idle.
      }
    }
  };

  // A manual edit while recording stops recognition — the person took the
  // keyboard back, and finals arriving after that would fight their typing.
  useEffect(() => {
    if (recording && draft !== composedRef.current) stop();
  }, [draft, recording]);

  // Unmount (switching threads, closing a pane) must not leave a hot mic.
  useEffect(() => stop, []);

  const start = () => {
    const host = window as unknown as Record<string, unknown>;
    const Ctor = (host.SpeechRecognition ?? host.webkitSpeechRecognition) as
      | (new () => RecognitionLike)
      | undefined;
    if (!Ctor) return;
    stateRef.current = beginDictation(draft);
    composedRef.current = draft;
    let recognition: RecognitionLike;
    try {
      recognition = new Ctor();
    } catch {
      return;
    }
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = navigator.language;
    recognition.onresult = (event) => {
      const state = stateRef.current;
      if (!state) return;
      // The resultIndex contract: everything from there on is new or
      // revised; earlier entries were already folded in.
      const fresh: DictationResult[] = [];
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const result = event.results[i];
        fresh.push({
          transcript: result[0]?.transcript ?? "",
          isFinal: Boolean(result.isFinal),
        });
      }
      const next = applyResult(state, fresh);
      stateRef.current = next;
      const composed = composedDraft(next);
      // A keystroke can land between the last commit and this event, its
      // setDraft still queued where only a functional updater can see it —
      // so the write goes through `resultWrite`: speech only ever replaces
      // the draft dictation itself last wrote, never typed text. On a
      // mismatch the typed draft survives and the stop-on-edit effect above
      // ends the session (draft ≠ the composed ref advanced here).
      const previous = composedRef.current;
      composedRef.current = composed;
      setDraft((current) => resultWrite(current, previous, composed));
    };
    // No toast on error, deliberately: CI Chrome's recognizer fails with a
    // network error while the constructor exists, and a user-facing alarm
    // for "your browser half-supports this" helps nobody. Idle says it all.
    recognition.onerror = () => stop();
    recognition.onend = () => stop();
    recognitionRef.current = recognition;
    setRecording(true);
    try {
      recognition.start();
    } catch {
      stop();
    }
  };

  if (!supported) return null;
  return (
    <button
      type="button"
      className={
        recording ? "composer-chip mic-button recording" : "composer-chip mic-button"
      }
      aria-pressed={recording}
      aria-label={recording ? "Stop dictation" : "Dictate"}
      title={
        recording
          ? "Stop dictation"
          : "Dictate into the composer — speech stays in your browser"
      }
      onClick={() => (recording ? stop() : start())}
    >
      <Mic size={14} />
    </button>
  );
}

/**
 * The files this thread is about, as chips above the composer.
 *
 * Every attachment the thread has, not only the unsent ones. A file attached
 * three turns ago is still in play — it is still quoted into every turn and
 * still retrievable — so a strip that showed only what was staged would say
 * the thread had forgotten a file it is actively using.
 *
 * A document chip is a button because there is somewhere to go: it opens the
 * file in a column beside the chat. A source chip is not, because a PDF has no
 * editor to open and a control that looked identical but did nothing would be
 * worse than no control.
 */
function AttachmentStrip({
  attachments,
  detach,
  openFile,
}: {
  attachments: ChatAttachment[];
  detach?: (attachment: ChatAttachment) => Promise<void>;
  openFile?: (documentId: string, filename: string) => void;
}) {
  if (attachments.length === 0) return null;
  return (
    <div className="attachment-strip" aria-label="Files attached to this chat">
      {attachments.map((attachment) => {
        const editable = attachment.kind === "document" && Boolean(openFile);
        // The preview branch reads the server-decided mime the list route
        // joined in — never the filename's extension (the `kind` doctrine).
        // A document arrives as ""/0 and lands on "chip": its preview is the
        // editor the chip already opens.
        const preview = previewKindOf(attachment.media_type, attachment.byte_size);
        const remove = detach && (
          <button
            type="button"
            className="attachment-chip-remove"
            onClick={() => void detach(attachment)}
            aria-label={`Remove ${attachment.filename} from this chat`}
          >
            <X size={12} />
          </button>
        );
        if (preview === "image") {
          return (
            <span
              className="attachment-chip attachment-preview"
              key={attachment.id}
              title={attachment.filename}
            >
              <ImageThumb
                sourceId={attachment.target_id}
                filename={attachment.filename}
              />
              {remove}
            </span>
          );
        }
        if (preview === "pdf") {
          return (
            <span
              className="attachment-chip attachment-preview"
              key={attachment.id}
              title={attachment.filename}
            >
              <PdfCard
                filename={attachment.filename}
                byteSize={attachment.byte_size}
              />
              {remove}
            </span>
          );
        }
        return (
          <span
            className={
              preview === "csv"
                ? "attachment-chip with-peek"
                : editable
                  ? "attachment-chip editable"
                  : "attachment-chip"
            }
            key={attachment.id}
          >
            {editable ? (
              <button
                type="button"
                className="attachment-chip-open"
                onClick={() => openFile?.(attachment.target_id, attachment.filename)}
                title={`Open ${attachment.filename}`}
              >
                <Paperclip size={12} />
                {attachment.filename}
              </button>
            ) : (
              <span className="attachment-chip-open" title={attachment.filename}>
                <Paperclip size={12} />
                {attachment.filename}
              </span>
            )}
            {remove}
            {/* The peek renders under the chip and disappears silently on
                any fetch or parse failure — the chip above stays either way,
                so a preview is décor, never an error surface. */}
            {preview === "csv" && (
              <CsvPeek
                sourceId={attachment.target_id}
                filename={attachment.filename}
              />
            )}
          </span>
        );
      })}
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

/**
 * The thumbs beside an assistant message's Copy button. Up posts at once;
 * down opens a small anchored popover collecting an optional note first. The
 * note state lives HERE, inside the leaf, so closing the popover (or the
 * message re-rendering) never greets the next open half-typed. Active state
 * reads from `myFeedback` — the transcript's own field — so a reload shows
 * the same verdict the click did.
 */
function FeedbackButtons({
  messageId,
  myFeedback,
  feedback,
}: {
  messageId: string;
  myFeedback: "" | "up" | "down";
  feedback: (
    messageId: string,
    verdict: "up" | "down",
    note: string,
  ) => Promise<void>;
}) {
  const [noteOpen, setNoteOpen] = useState(false);
  const [note, setNote] = useState("");
  return (
    <span className="feedback-buttons">
      <button
        type="button"
        className={
          myFeedback === "up" ? "copy-button feedback-active" : "copy-button"
        }
        aria-label="Good answer"
        aria-pressed={myFeedback === "up"}
        onClick={() => {
          setNoteOpen(false);
          void feedback(messageId, "up", "");
        }}
      >
        <ThumbsUp size={13} />
      </button>
      <button
        type="button"
        className={
          myFeedback === "down" ? "copy-button feedback-active" : "copy-button"
        }
        aria-label="Bad answer"
        aria-pressed={myFeedback === "down"}
        aria-expanded={noteOpen}
        onClick={() => setNoteOpen((value) => !value)}
      >
        <ThumbsDown size={13} />
      </button>
      {noteOpen && (
        <div className="feedback-note-pop">
          <textarea
            value={note}
            onChange={(event) => setNote(event.target.value)}
            rows={2}
            maxLength={2000}
            aria-label="What went wrong? (optional)"
            placeholder="What went wrong? (optional)"
            autoFocus
          />
          <button
            type="button"
            className="ghost-button"
            onClick={() => {
              void feedback(messageId, "down", note.trim());
              setNoteOpen(false);
              setNote("");
            }}
          >
            Send
          </button>
        </div>
      )}
    </span>
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

function ToolCallCard({
  call,
  decide,
  todos,
  pinning,
}: {
  call: AgentToolCall;
  decide: ToolDecision;
  todos?: { lists: Board[]; ops: TodoOps; selfId?: string };
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
  // Why the card exists at all, when the provenance gate is what raised it.
  // Only on a call still waiting: once decided, "it is waiting for you" is
  // past tense and the trail already records what happened.
  const gateNote = pending && call.gate_reason ? describeGate(call.gate_reason) : "";
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
      {/* Above the preview, deliberately: the reason this is waiting has to be
          read before the diff it is waiting on. */}
      {gateNote && (
        <p className="tool-gate-note">
          <ShieldAlert size={12} aria-hidden="true" />
          {gateNote}
        </p>
      )}
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
          selfId={todos.selfId}
          compact
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
              placeholder="Answer (optional)"
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
  regenerate,
  editMessage,
  viewerId,
  decideAgentCall,
  openCitation,
  seedDraft,
  showCoverage = false,
  attach,
  approval,
  unrestricted,
  todos,
  pinning,
  endRef,
  selectedAgentId,
  onSelectAgent,
  turnControls,
  skills,
  thinking,
  plan,
  responseStyle,
  feedback,
  fork,
  undo,
  openView,
  scheduleDraft,
  incognito,
  coworking,
  conversationId,
}: ChatViewProps) {
  // The shared-thread presence surface, "" everywhere presence has no
  // business: personal threads, panels mounted without the channel.
  const presenceSurface =
    sharedThread && coworking && conversationId
      ? `conversation:${conversationId}`
      : "";
  const typers = presenceSurface
    ? typersOn(coworking!.presences, presenceSurface, viewerId ?? "")
    : [];
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
  // The "+" tool menu, open or not — local like `attachOpen` and for the same
  // reason: nothing outside the composer cares.
  const [plusOpen, setPlusOpen] = useState(false);
  // Which message is an editor right now, and what it says. View state (not
  // row state) so exactly one edit can be open at a time — the rail's rename
  // pattern. Deliberately no on-blur submit anywhere below: an edit deletes
  // everything after the message, and a destructive act must never ride a
  // stray click.
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState("");
  const [editSaving, setEditSaving] = useState(false);
  // The transcript scroll container is the virtualizer's scroll element. The
  // virtualizer only engages past 50 messages: below that the list is short
  // enough that mounting every row is cheaper and friendlier to the jsdom test
  // suite, and the streaming auto-scroll and edit-in-place paths stay exactly
  // as they were. Past 50, only the visible window is mounted and measured as
  // it scrolls into view — the Vercel "virtualize large lists" rule, applied
  // only where it actually pays.
  const scrollRef = useRef<HTMLDivElement>(null);
  const virtualize = messages.length > 50;
  const rows = useVirtualizer({
    count: messages.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 160,
    overscan: 6,
  });
  const submitEdit = async (messageId: string) => {
    // The button is aria-disabled rather than disabled while a run streams —
    // focus must survive the wait — so the guard lives here too. `editSaving`
    // is the in-flight half: `activeRun` only becomes true AFTER the edit's
    // round trip (followRun sets it), so without this a held Enter's key
    // repeat would fire concurrent truncations at the same pivot.
    if (!editMessage || !editDraft.trim() || activeRun || editSaving) return;
    setEditSaving(true);
    try {
      const accepted = await editMessage(messageId, editDraft.trim());
      // Success closes the editor; failure keeps the rewritten words on screen.
      if (accepted) setEditingId(null);
    } finally {
      setEditSaving(false);
    }
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
      // plan mode by hand lands on THIS member's default: for someone in Safe
      // mode that is still "ask before writes", and for everyone else it is
      // where their threads live, which is the landing that does not feel like
      // the product changed its mind on the way out of a mode.
      approval.setMode(
        approval.mode === "plan"
          ? approval.safeMode
            ? "ask_writes"
            : "auto_writes"
          : "plan",
      );
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
  // Writes are going through unreviewed — either kind. This is what decides
  // whether the trail is SHOWN at all, and it is deliberately unchanged by the
  // default flip: the record of what ran without asking is the thing that makes
  // an agentic default honest, so it renders whenever it is true.
  const autoApproving = Boolean(approval && isBypass(approval.mode)) || Boolean(unrestricted);
  // Whether that fact is a DEPARTURE, which is what earns the warning
  // treatment: the composer's alarm styling and the "Turn off" button. The
  // development bypass always is (it is wider than any thread's mode and
  // nobody chose it per thread); a thread's own mode is one only for a member
  // running Safe mode, who asked to be asked and is not being.
  const bypassed =
    Boolean(approval && approval.safeMode && isBypass(approval.mode)) ||
    Boolean(unrestricted);
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

  // One message row, shared by the plain list and the virtualized window so the
  // two paths can never drift. `articleProps` carries the virtualizer's ref,
  // data-index and absolute-positioning style; the plain path passes nothing
  // and the article lays out in normal flow exactly as it always has.
  const renderMessage = (
    message: Message,
    articleProps?: {
      ref?: (el: HTMLElement | null) => void;
      "data-index"?: number;
      style?: CSSProperties;
    },
  ) => {
    // An aside ("/btw") is a user message with no run — a note the agent will
    // read later, not a prompt it answered — so it wears a quieter treatment
    // than a turn, and it never grows a pencil: there is no turn after it to
    // re-run.
    const aside = message.role === "user" && message.run_id === "";
    const editable =
      Boolean(editMessage) &&
      !aside &&
      senderIsViewer(message, Boolean(sharedThread), viewerId ?? "");
    return (
      <article
        key={message.id}
        className={`message ${message.role}${aside ? " aside" : ""}`}
        ref={articleProps?.ref}
        data-index={articleProps?.["data-index"]}
        style={articleProps?.style}
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
          {/* Never on the streaming placeholder: its `streaming-<runId>` id
              names a row no server has, so a thumb clicked before
              message.completed would POST a fake id, 404, and roll back
              with the red toast. The real row takes its place on settle. */}
          {feedback &&
            message.role === "assistant" &&
            message.id &&
            !isStreamingMessage(message.id) && (
              <FeedbackButtons
                messageId={message.id}
                myFeedback={message.my_feedback ?? ""}
                feedback={feedback}
              />
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
              {/* Said before the button is pressed, not discovered from its
                  result. This is the one hint the minimal pass kept on this
                  surface: it names an irreversible consequence rather than
                  teaching an obvious control. */}
              <p className="message-edit-note">
                Saving re-runs the thread from here — the old answer and
                everything after it are deleted.
              </p>
              <div className="message-edit-actions">
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
                  // `editSaving` is the in-flight half: `activeRun` only becomes
                  // true AFTER the edit's round trip (followRun sets it), so
                  // without it a held Enter's key repeat fires concurrent
                  // truncations at the same pivot. submitEdit refuses all three.
                  disabled={!editDraft.trim() || Boolean(activeRun) || editSaving}
                  onClick={() => void submitEdit(message.id)}
                >
                  {/* Named for what it does, not for the verb: this deletes the
                      old turn and everything after it, then re-runs. "Save"
                      alone reads as non-destructive and the truncation would be
                      a surprise — the one place on this surface where longer
                      copy is carrying information rather than teaching. */}
                  Save &amp; re-run
                </button>
              </div>
            </div>
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
        {message.role === "assistant" &&
          flaggedRuns?.includes(message.run_id) && <ScreenFlagNote />}
        {message.citation_report && (
          <>
            <CitationVerdictNote report={message.citation_report} />
            {/* Under the plate, not inside the answer: the per-sentence
                verdicts carry offsets, and decorating the prose in place would
                mean re-parsing the markdown this render just produced. */}
            <GroundingDrawer report={message.citation_report} />
          </>
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
        {/* Follow-up chips. An EMPTY list renders nothing at all rather than an
            empty shelf: "no suggestion cleared the retrieval probe" is a real
            and common answer for a thin corpus, and a labelled-but-empty row
            would read as a failure. Each chip SEEDS the composer — see
            `seedDraft` — because a chip that sent would make a stray click
            spend a turn. */}
        {/* What this run actually looked at. Collapsed and fetched on demand —
            only a plan-mode run or a deliverable run records a ledger, so
            asking eagerly would fire one 404 per message in the transcript. */}
        {message.run_id && showCoverage && <CoverageDrawer runId={message.run_id} />}
        {orderFollowups(message.followups ?? []).length > 0 && (
          <div className="followups">
            {orderFollowups(message.followups ?? []).map((followup) => (
              <button
                key={followup.text}
                type="button"
                className="followup-chip"
                title={describeFollowup(followup)}
                onClick={() => seedDraft?.(followup.text)}
              >
                {followup.text}
              </button>
            ))}
          </div>
        )}
      </article>
    );
  };

  // Built once so the shared and personal paths can never drift; only the
  // shared thread wraps it in the pointer layer below.
  const transcript = (
      <div
        ref={scrollRef}
        className={`message-scroll ${messages.length === 0 ? "empty" : ""}`}
      >
        {/* An empty transcript is just the composer. The starter cards that
            used to teach here were removed deliberately: they were onboarding
            filler on a surface whose only real instruction is the placeholder
            in the composer itself. */}
        {messages.length > 0 && (
          <div className="message-column">
            {virtualize
              ? rows.getTotalSize() > 0 && (
                  <div
                    style={{
                      position: "relative",
                      width: "100%",
                      height: rows.getTotalSize(),
                    }}
                  >
                    {rows.getVirtualItems().map((item) =>
                      renderMessage(messages[item.index], {
                        ref: rows.measureElement,
                        "data-index": item.index,
                        style: {
                          position: "absolute",
                          top: 0,
                          left: 0,
                          width: "100%",
                          transform: `translateY(${item.start}px)`,
                          // The plain path spaces rows with `.message { margin-bottom:
                          // 32px }`. Margins don't fold into a measured height, so
                          // the virtualized row carries the gap as padding instead —
                          // same visual gap, included in the measured size.
                          marginBottom: 0,
                          paddingBottom: 32,
                        },
                      }),
                    )}
                  </div>
                )
              : messages.map((message) => renderMessage(message))}
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
            {/* The plan's own lane, in the same slot and for the same reason:
                live narration of what this turn committed to doing, cleared
                when the run settles. */}
            {activeRun && <PlanTrail plan={plan ?? null} />}
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
  );

  return (
    <section className="chat-layout">
      {/* Mounted ONLY for shared threads: a personal thread never emits a
          pointer beat, and reflowing message lists make the fractional
          positions approximate there — the documented layer trade. */}
      {presenceSurface ? (
        <LiveCursorLayer
          surface={presenceSurface}
          coworking={coworking}
          className="chat-cursor-box"
        >
          {transcript}
        </LiveCursorLayer>
      ) : (
        transcript
      )}

      <div
        className={[
          "composer-zone",
          bypassed ? "bypassed" : "",
          incognito?.on ? "incognito" : "",
        ]
          .filter(Boolean)
          .join(" ")}
      >
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
          autoApproving && (
            <BypassIndicator
              conversationTitle={approval.conversationTitle}
              approved={autoApprovedCalls(agentCalls, approval.conversationId)}
              stop={() => approval.setMode("ask_writes")}
              // Warning when it departs from what this member asked for;
              // otherwise the same trail, said in an indoor voice.
              tone={bypassed ? "warning" : "notice"}
            />
          )
        )}
        {/* Above the composer, in the non-scrolling zone like the banners: a
            state the user chose, restated where the words it governs are
            typed, so a temporary chat can never be mistaken for one that is
            learning. */}
        {incognito?.on && (
          <p className="incognito-note" role="status">
            <EyeOff size={12} aria-hidden="true" />
            Temporary chat — the assistant won&apos;t remember this conversation.
          </p>
        )}
        {/* Whenever the thread is shared, not only while someone types: the
            line's height is reserved so the composer does not jump when a
            teammate starts. aria-live announces the change politely. */}
        {presenceSurface && (
          <p className="chat-typing" aria-live="polite">
            {typingLine(typers.map((presence) => presence.actor_label))}
          </p>
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
          {plusOpen && (
            <PlusMenu
              rows={[
                ...(attach
                  ? [{ label: "Attach a file", run: () => setAttachOpen(true) }]
                  : []),
                ...(skills && !skills.attached && !activeRun
                  ? [
                      {
                        label: "Use a skill",
                        // The Skills chip's exact handler: "/" in the draft
                        // opens the picker; this row is only a second door.
                        run: () => {
                          if (!draft.startsWith("/")) setDraft(`/${draft}`);
                        },
                      },
                    ]
                  : []),
                // Always rendered, DISABLED without text — a row that
                // appears only when text exists is a moving menu, but an
                // empty draft has nothing to schedule (the Crons composer
                // opens only over a seed), so the row says why instead of
                // silently navigating to a closed composer.
                ...(scheduleDraft
                  ? [
                      {
                        label: "Do this on a schedule…",
                        run: () => scheduleDraft(draft),
                        disabled: !draft.trim(),
                        title: draft.trim()
                          ? undefined
                          : "Type the prompt to schedule first",
                      },
                    ]
                  : []),
                ...(openView
                  ? [
                      { label: "Sources", run: () => openView("sources") },
                      { label: "Datasets", run: () => openView("datasets") },
                      { label: "Gallery", run: () => openView("gallery") },
                    ]
                  : []),
              ]}
              close={() => setPlusOpen(false)}
            />
          )}
          {attach && attachOpen && (
            <AttachMenu attach={attach} close={() => setAttachOpen(false)} />
          )}
          {attach?.attachments && (
            <AttachmentStrip
              attachments={attach.attachments}
              detach={attach.detach}
              openFile={attach.openFile}
            />
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
            {/* The "+" menu duplicates the Attach/Skills chips by design: the
                menu is the discoverable index, the chips are the fast path.
                Consolidating them would regress the labelled-chips work above. */}
            <button
              type="button"
              className={plusOpen ? "composer-chip plus on" : "composer-chip plus"}
              onClick={() => setPlusOpen((value) => !value)}
              aria-expanded={plusOpen}
              aria-label="Open tools"
            >
              <Plus size={14} />
            </button>
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
            {/* Feature-detected on mount, so it renders nowhere the API is
                absent (and never at SSR). Dictation is client-only. */}
            <MicButton draft={draft} setDraft={setDraft} />
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
            {responseStyle && (
              /* "· you" mirrors effort's "· this thread": this preference
                 follows the member across every thread, and the scope is part
                 of the name (the Foyer trust rule). "Custom" is offered only
                 once custom text exists — the composer picker selects, it
                 does not author (that stays in the settings menu). */
              <select
                className="composer-select"
                value={responseStyle.preset}
                onChange={(event) =>
                  responseStyle.onChange(
                    event.target.value as StylePreset,
                    responseStyle.customText,
                  )
                }
                aria-label="Response style · you"
                title="Your response style — applies to your turns in every thread"
              >
                <option value="normal">Normal</option>
                <option value="concise">Concise</option>
                <option value="explanatory">Explanatory</option>
                <option value="formal">Formal</option>
                {(responseStyle.customText.trim() !== "" ||
                  responseStyle.preset === "custom") && (
                  <option value="custom">Custom</option>
                )}
              </select>
            )}
            {approval && (
              <ApprovalModeControl mode={approval.mode} setMode={approval.setMode} />
            )}
            {incognito && (
              // Disabled once the thread exists: incognito is stamped at
              // creation (flipping it later would misdescribe turns that
              // already ran), so on a live thread the chip only reports.
              <button
                type="button"
                className={
                  incognito.on ? "composer-chip incognito-chip on" : "composer-chip incognito-chip"
                }
                onClick={incognito.toggle}
                disabled={!incognito.toggle}
                aria-pressed={incognito.on}
                aria-label="Incognito"
                title={
                  incognito.toggle
                    ? "Start this thread as a temporary chat — it won't read or write memory"
                    : incognito.on
                      ? "A temporary chat — this thread doesn't read or write memory"
                      : "Set when a chat is created — use “New temporary chat” for the next one"
                }
              >
                <EyeOff size={14} />
                Incognito
              </button>
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
