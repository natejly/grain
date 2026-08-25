"use client";

import type {
  AgentToolCall,
  ApprovalMode,
  Conversation,
  ConversationDefaults,
  Message,
  Skill,
} from "@workspace/api-client";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { createThreadHandlers } from "./handlers/thread";
import type { BudgetPark } from "./views/budget-format";
import { describeError } from "./views/shared";

export type ConversationThreadDeps = {
  /** The conversation this pane is bound to, chosen from the rail. */
  conversationId: string;
  /** The workspace default agent, from bootstrap; a per-pane pick overrides it. */
  defaultAgentId?: string;
  /** The deployment's default reasoning effort, seeded into this pane's composer once. */
  defaultEffort?: string;
  /**
   * What the bound thread remembers (Conversation.default_*), seeding this
   * pane's pickers when it opens — and a pick here writes back through
   * `setConversationDefaults`, so the pane and the rail remember the same
   * thing. Absent means "seed nothing", which is the old per-pane behaviour.
   */
  threadDefaults?: { agentId: string; model: string; effort: string };
  /**
   * The pane's own run finished. It refreshes its own transcript and tool cards
   * itself; this is only the workspace-wide catch-up it cannot do alone — a
   * re-read of the conversations list so the rail shows the new title/timestamp.
   * Deliberately narrow: an extra pane is a satellite, not the shell, so it does
   * NOT own the eight-collection refresh the primary chat performs.
   */
  onSettled?: () => Promise<void> | void;
  /** The approval mode changed on the server; hand the updated row back to the
   *  one place it lives (the shell's conversations list) so this pane and the
   *  rail cannot disagree about the mode in force. */
  onApprovalChanged?: (updated: Conversation) => void;
};

/**
 * One chat pane's thread — an independent conversation rendered beside the
 * shell's primary chat in the multi-pane split.
 *
 * A conversation-keyed sibling of `useDocumentThread`: same shape, same shared
 * turn engine (`createThreadHandlers`), but keyed on a conversation id chosen
 * from the rail rather than resolved from a document, and carrying the full
 * per-turn composer controls (agent / model / effort / fast / skill) the
 * document panel omits. Everything is per-hook-instance `useState`/`useRef` —
 * nothing at module scope — so N panes each get their own messages, run,
 * budget hold and composer, and a stream or approval in one cannot bleed into
 * another. That isolation is the whole point of instantiating this per pane.
 */
export function useConversationThread({
  conversationId,
  defaultAgentId,
  defaultEffort,
  threadDefaults,
  onSettled,
  onApprovalChanged,
}: ConversationThreadDeps) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [agentCalls, setAgentCalls] = useState<AgentToolCall[]>([]);
  const [draft, setDraft] = useState("");
  const [activeRun, setActiveRun] = useState<string | null>(null);
  // The latest run id, mirrored into a ref so the unmount cleanup below reads
  // the run that is live *when the pane closes* rather than the null it closed
  // over at mount.
  const activeRunRef = useRef<string | null>(null);
  const [runStatus, setRunStatus] = useState("");
  const [budgetPark, setBudgetPark] = useState<BudgetPark | null>(null);
  const [error, setError] = useState("");
  const endRef = useRef<HTMLDivElement>(null);
  // Always this pane's conversation, so `createThreadHandlers`' `stillOpen`
  // guard keeps this pane's deltas in this pane. Each pane has its own ref
  // instance, which is what isolates the streams.
  const conversationRef = useRef<string | null>(conversationId);
  /**
   * The conversation the pane is loading for. A pane whose bound id changes must
   * not paint the previous conversation's history: a response fetched for the id
   * we just left is discarded when this no longer names it.
   */
  const loadingFor = useRef("");

  // Per-turn composer controls, this pane's own — a model, effort, fast or skill
  // picked in pane A must never touch pane B. Agent/model/effort seed from what
  // the bound THREAD remembers (`threadDefaults`) and a pick writes back, so
  // pane and rail agree; fast and the skill stay per-turn session state.
  const [selectedAgentId, setSelectedAgentId] = useState("");
  const [selectedModel, setSelectedModel] = useState("");
  const [selectedEffort, setSelectedEffort] = useState("");
  const [fast, setFast] = useState(false);
  const [attachedSkill, setAttachedSkill] = useState<Skill | null>(null);
  const [skillArgs, setSkillArgs] = useState<Record<string, unknown>>({});

  // Seed the effort from the deployment default the first time it arrives and
  // never again, exactly as the shell does — `current || preset` leaves a value
  // the user has since picked alone.
  useEffect(() => {
    if (defaultEffort) setSelectedEffort((current) => current || defaultEffort);
  }, [defaultEffort]);

  // The row's defaults, readable by the id-keyed effect below without joining
  // its dependencies: the rail refreshes the row often, and re-seeding on every
  // refresh would stomp a pick made after the pane opened. Declared before that
  // effect so the first seed reads a current value.
  const threadDefaultsRef = useRef(threadDefaults);
  useEffect(() => {
    threadDefaultsRef.current = threadDefaults;
  }, [threadDefaults]);
  // Same ref treatment for the deployment default: the reset effect below must
  // key on the conversation id ALONE — bootstrap arriving late must not wipe a
  // pane's live transcript just to reconsider an effort fallback.
  const defaultEffortRef = useRef(defaultEffort);
  useEffect(() => {
    defaultEffortRef.current = defaultEffort;
  }, [defaultEffort]);

  useEffect(() => {
    loadingFor.current = conversationId;
    conversationRef.current = conversationId;
    setMessages([]);
    setAgentCalls([]);
    setError("");
    // Seed the pickers from what this thread remembers — once per binding,
    // exactly like the transcript reset around it.
    const remembered = threadDefaultsRef.current;
    if (remembered) {
      setSelectedAgentId(remembered.agentId);
      setSelectedModel(remembered.model);
      setSelectedEffort(remembered.effort || defaultEffortRef.current || "");
      setFast(false);
    }
    // The run belonged to the id we just left; leaving it set would wire this
    // pane's Stop to somebody else's run and strand a budget hold on screen.
    setActiveRun(null);
    setRunStatus("");
    setBudgetPark(null);
    void (async () => {
      try {
        const [history, calls] = await Promise.all([
          api.listMessages(conversationId),
          api.listAgentToolCalls(),
        ]);
        if (loadingFor.current !== conversationId) return;
        setMessages(history);
        setAgentCalls(calls);
      } catch (caught) {
        if (loadingFor.current !== conversationId) return;
        setError(describeError(caught, "Could not open this conversation"));
      }
    })();
  }, [conversationId]);

  useEffect(() => {
    activeRunRef.current = activeRun;
  }, [activeRun]);

  // Closing an extra pane tears this hook down. A run still streaming here would
  // otherwise keep burning budget on the server with no pane left to read it, so
  // on unmount cancel it best-effort and neutralise the stream guard — clearing
  // `conversationRef` makes `stillOpen()` false, so any events already in flight
  // stop painting into a pane that is gone. Empty deps: cleanup runs on unmount
  // only, and the ref (not the closed-over `activeRun`) supplies the live run.
  useEffect(() => {
    return () => {
      const run = activeRunRef.current;
      conversationRef.current = null;
      if (run) void api.cancelRun(run).catch(() => undefined);
    };
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [messages, runStatus]);

  /**
   * The composer's slash-picker actions, this pane's own copy of the shell's:
   * attaching seeds each declared arg with its default, clearing drops both.
   */
  const attachSkill = useCallback((skill: Skill) => {
    setAttachedSkill(skill);
    const seeded: Record<string, unknown> = {};
    for (const arg of skill.args) {
      if (arg.default !== null && arg.default !== undefined) seeded[arg.name] = arg.default;
    }
    setSkillArgs(seeded);
  }, []);
  const detachSkill = useCallback(() => {
    setAttachedSkill(null);
    setSkillArgs({});
  }, []);
  const setSkillArg = useCallback((name: string, value: unknown) => {
    setSkillArgs((current) => ({ ...current, [name]: value }));
  }, []);

  const onRunSettled = useCallback(async () => {
    // This pane's own tool cards, then the workspace-wide list refresh the pane
    // cannot do for itself. Nothing more: an extra pane has no sidebar, project
    // pane or infra list of its own to catch up — that is the primary's job.
    setAgentCalls(await api.listAgentToolCalls().catch(() => []));
    await Promise.resolve(onSettled?.()).catch(() => undefined);
  }, [onSettled]);

  const thread = createThreadHandlers({
    agentId: selectedAgentId || defaultAgentId,
    // Fast omits the effort so the backend's fast→low mapping wins; an empty
    // model/effort is the deployment default; an unset skill is today's behaviour.
    controls: {
      model: selectedModel,
      effort: fast ? "" : selectedEffort,
      fast,
      skillId: attachedSkill?.id,
      skillArgs,
    },
    messages,
    draft,
    activeConversation: conversationId,
    activeRun,
    setError,
    setMessages,
    setAgentCalls,
    setActiveRun,
    setRunStatus,
    setBudgetPark,
    setDraft,
    activeConversationRef: conversationRef,
    // Never creates: the pane is bound to an existing conversation.
    ensureConversation: async () => conversationId,
    // The skill attachment is per-turn; drop it once the send is accepted.
    onSent: detachSkill,
    onRunSettled,
  });

  /**
   * Change how much this pane's thread asks before acting.
   *
   * The updated Conversation goes back to the shell's list (its one home), so
   * the pane's picker reflects the same row the rail does rather than a private
   * copy that can drift. The server re-reads the mode per tool call.
   */
  const setApprovalMode = useCallback(
    async (mode: ApprovalMode) => {
      setError("");
      try {
        const updated = await api.setApprovalMode(conversationId, mode);
        onApprovalChanged?.(updated);
      } catch (caught) {
        setError(describeError(caught, "Could not change the approval mode"));
      }
    },
    [conversationId, onApprovalChanged],
  );

  /**
   * A pick is remembered on the thread, exactly as the shell composer does it;
   * the response row rides the same channel the approval mode uses back to the
   * shell's list — one home for the row, so pane and rail cannot disagree.
   * Best-effort on the wire: the pick already governs this pane either way.
   */
  const rememberThreadDefault = useCallback(
    (patch: ConversationDefaults) => {
      const id = conversationRef.current;
      if (!id) return;
      void api
        .setConversationDefaults(id, patch)
        .then((updated) => onApprovalChanged?.(updated))
        .catch(() => undefined);
    },
    [onApprovalChanged],
  );
  const pickAgent = useCallback(
    (value: string) => {
      setSelectedAgentId(value);
      rememberThreadDefault({ default_agent_id: value });
    },
    [rememberThreadDefault],
  );
  const pickModel = useCallback(
    (value: string) => {
      setSelectedModel(value);
      rememberThreadDefault({ default_model: value });
    },
    [rememberThreadDefault],
  );
  const pickEffort = useCallback(
    (value: string) => {
      setSelectedEffort(value);
      rememberThreadDefault({ default_effort: value });
    },
    [rememberThreadDefault],
  );

  return {
    messages,
    agentCalls,
    draft,
    setDraft,
    activeRun,
    runStatus,
    budgetPark,
    error,
    endRef,
    selectedAgentId,
    setSelectedAgentId: pickAgent,
    selectedModel,
    setSelectedModel: pickModel,
    selectedEffort,
    setSelectedEffort: pickEffort,
    fast,
    setFast,
    attachedSkill,
    skillArgs,
    attachSkill,
    detachSkill,
    setSkillArg,
    setApprovalMode,
    ...thread,
  };
}
