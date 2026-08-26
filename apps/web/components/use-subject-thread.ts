"use client";

import type { AgentToolCall, ApprovalMode, Message } from "@workspace/api-client";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { createThreadHandlers } from "./handlers/thread";
import type { BudgetPark } from "./views/budget-format";
import { describeError } from "./views/shared";

/** The three things a chat panel can be *about*. */
export type SubjectKind = "document" | "project" | "dashboard";

export type SubjectThreadDeps = {
  kind: SubjectKind;
  /** The open subject, or "" when none is. */
  subjectId: string;
  /** The agent to run as, from bootstrap. */
  agentId?: string;
  /**
   * Which part of the subject is on screen — the path the project editor has
   * open. Sent with each message and stored on the run, so a turn that parks
   * for an approval resumes against the file it was asked about.
   */
  focus?: string;
  /** Everything outside the thread a finished run may have changed. */
  onRunSettled?: () => Promise<void>;
  /** A write parked mid-stream and the caller renders the review inline. */
  onToolProposed?: () => Promise<void>;
};

/**
 * The chat thread that belongs to one subject: a document, a project, a chart.
 *
 * Its own state, not the shell's: the rail's thread and this one can both be
 * mid-run, and sharing `activeRun` between them would have a document edit's
 * spinner stop the composer in Chat. What it does *not* have its own copy of is
 * the turn — that is `createThreadHandlers`, the same code the rail runs.
 *
 * One hook for all three surfaces rather than three, for the reason
 * `createThreadHandlers` was extracted in the first place: forking it would mean
 * streaming, cancel, tool cards and approvals drifting apart one fix at a time,
 * with the third and fourth surface always a few bugs behind the first. The
 * only thing that differs between them is what a settled run should re-read,
 * which is why that is a callback and not a fourth copy of this file.
 *
 * The thread is fetched rather than created: `POST /api/{kind}s/{id}/
 * conversation` is get-or-create keyed on the subject, so a remount, a second
 * tab and a StrictMode double-invoke all land on the same conversation. That is
 * why there is no "have I already made one" flag here — there is nothing to
 * guard, which is the point of putting the key on the server side.
 */
export function useSubjectThread({
  kind,
  subjectId,
  agentId,
  focus,
  onRunSettled,
  onToolProposed,
}: SubjectThreadDeps) {
  const [conversationId, setConversationId] = useState<string | null>(null);
  // The subject thread's approval mode — a real Conversation column, so the
  // panel can carry the same control the rail composer does. Subject panels
  // used to HIDE the control, which meant a subject thread's mode was
  // unreadable and unchangeable from the one surface that uses it.
  // Seeded with the product default rather than the strict mode: this is a
  // placeholder the server's value replaces as soon as the thread loads, and a
  // placeholder that disagrees with what the thread will say is a control that
  // visibly changes its mind a beat after the panel opens.
  const [approvalMode, setApprovalModeState] = useState<ApprovalMode>("auto_writes");
  const [messages, setMessages] = useState<Message[]>([]);
  const [agentCalls, setAgentCalls] = useState<AgentToolCall[]>([]);
  const [draft, setDraft] = useState("");
  const [activeRun, setActiveRun] = useState<string | null>(null);
  const [runStatus, setRunStatus] = useState("");
  const [budgetPark, setBudgetPark] = useState<BudgetPark | null>(null);
  const [error, setError] = useState("");
  const endRef = useRef<HTMLDivElement>(null);
  const conversationRef = useRef<string | null>(null);
  /**
   * The subject the panel is loading for. A user who clicks a second file while
   * the first thread is still in flight must not get the first file's messages:
   * the response is discarded when this no longer names its subject. Keyed on
   * the pair, because a project and a dashboard could hold the same id.
   */
  const loadingFor = useRef("");
  const key = `${kind}:${subjectId}`;

  useEffect(() => {
    conversationRef.current = conversationId;
  }, [conversationId]);

  useEffect(() => {
    loadingFor.current = key;
    setConversationId(null);
    conversationRef.current = null;
    setApprovalModeState("auto_writes");
    setMessages([]);
    setAgentCalls([]);
    setDraft("");
    setError("");
    // The run belongs to the subject we just left. Leaving it set disables the
    // new subject's composer, turns Send into a Stop wired to somebody else's
    // run, and leaves a budget hold on screen for a thread that has none — the
    // panel would be reporting the previous subject's turn as this one's.
    setActiveRun(null);
    setRunStatus("");
    setBudgetPark(null);
    if (!subjectId) return;
    void (async () => {
      try {
        const conversation = await api.subjectConversation(kind, subjectId);
        const history = await api.listMessages(conversation.id);
        if (loadingFor.current !== key) return;
        setConversationId(conversation.id);
        conversationRef.current = conversation.id;
        // The whole row was always coming back; only the id used to be kept,
        // which is why the panel could not show its own thread's mode.
        setApprovalModeState(conversation.approval_mode);
        setMessages(history);
      } catch (caught) {
        if (loadingFor.current !== key) return;
        setError(describeError(caught, "Could not open the chat for this"));
      }
    })();
  }, [key, kind, subjectId]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [messages, runStatus]);

  const settled = useCallback(async () => {
    await onRunSettled?.().catch(() => undefined);
    setAgentCalls(await api.listAgentToolCalls().catch(() => []));
  }, [onRunSettled]);

  const thread = createThreadHandlers({
    agentId,
    // Only the focus rides here; model/effort/skill belong to the rail's
    // composer, which these panels deliberately do not have.
    controls: focus ? { subjectFocus: focus } : undefined,
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
    // Never creates: the thread is the subject's, and a panel that has not
    // resolved it yet has nothing to send into.
    ensureConversation: async () => {
      if (!conversationId) throw new Error("This has no chat thread yet");
      return conversationId;
    },
    onToolProposed,
    onRunSettled: settled,
  });

  /**
   * Change how much this subject's thread asks before acting. The server is
   * the row's one home; the panel keeps only the mode it answered with, and
   * discards a reply that lands after the panel moved to another subject.
   */
  const setApprovalMode = useCallback(async (mode: ApprovalMode) => {
    const id = conversationRef.current;
    if (!id) return;
    setError("");
    try {
      const updated = await api.setApprovalMode(id, mode);
      if (conversationRef.current === id) {
        setApprovalModeState(updated.approval_mode);
      }
    } catch (caught) {
      setError(describeError(caught, "Could not change the approval mode"));
    }
  }, []);

  return {
    conversationId,
    approvalMode,
    setApprovalMode,
    messages,
    agentCalls,
    draft,
    setDraft,
    activeRun,
    runStatus,
    budgetPark,
    error,
    setError,
    endRef,
    ...thread,
  };
}
