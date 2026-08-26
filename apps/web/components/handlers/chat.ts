"use client";

import type {
  AgentToolCall,
  ApprovalMode,
  Bootstrap,
  Conversation,
  DocumentVersion,
  Message,
  Skill,
  WorkspaceDocument,
  WorkspaceProject,
} from "@workspace/api-client";
import type { Dispatch, MouseEvent, RefObject, SetStateAction } from "react";
import { api } from "../api";
import type { BudgetPark } from "../views/budget-format";
import { describeError, type View } from "../views/shared";
import { UNDO_CONFIRM, summarizeUndo } from "../views/undo-format";
import { createThreadHandlers } from "./thread";

/**
 * The Chat *rail*: which conversations exist, which one is open, and what the
 * shell has to catch up on when one of their runs ends.
 *
 * The turn itself — send, stream, tool cards, approvals, cancel, regenerate —
 * is `createThreadHandlers`, shared with the panel beside a document. What is
 * left here is exactly what the two surfaces do not share.
 */
export type ChatHandlerDeps = {
  bootstrap: Bootstrap | null;
  selectedAgentId: string;
  setSelectedAgentId: Dispatch<SetStateAction<string>>;
  /** Per-turn composer overrides; `fast` omits the effort so the backend's
   * fast→low mapping applies, and an empty model/effort is the deployment default. */
  selectedModel: string;
  selectedEffort: string;
  fast: boolean;
  /** The Thinking toggle: stream this turn's reasoning summaries as a trail. */
  thinking: boolean;
  /** The skill attached to the next turn, its arg values, and the way to drop
   * it once the send lands. All three are per-turn, like the draft. */
  attachedSkill: Skill | null;
  skillArgs: Record<string, unknown>;
  clearAttachedSkill: () => void;
  conversations: Conversation[];
  messages: Message[];
  draft: string;
  activeConversation: string | null;
  activeRun: string | null;
  setError: Dispatch<SetStateAction<string>>;
  /** The neutral toast — an outcome to read, not a failure. `sticky` keeps it
   * open: an undo's skipped half must not vanish on a four-second timer. */
  setNotice: (
    notice: { text: string; at: number; sticky?: boolean } | null,
  ) => void;
  setView: Dispatch<SetStateAction<View>>;
  setSidebarOpen: Dispatch<SetStateAction<boolean>>;
  setConversations: Dispatch<SetStateAction<Conversation[]>>;
  setActiveConversation: Dispatch<SetStateAction<string | null>>;
  setMessages: Dispatch<SetStateAction<Message[]>>;
  setAgentCalls: Dispatch<SetStateAction<AgentToolCall[]>>;
  setActiveRun: Dispatch<SetStateAction<string | null>>;
  setRunStatus: Dispatch<SetStateAction<string>>;
  /** The live thinking trail the current run has streamed; "" between runs. */
  setRunThinking: Dispatch<SetStateAction<string>>;
  setBudgetPark: Dispatch<SetStateAction<BudgetPark | null>>;
  /** Records a run the prompt-injection screen flagged, so the transcript can mark it. */
  onScreenFlag: (runId: string) => void;
  setDraft: Dispatch<SetStateAction<string>>;
  setActiveProject: Dispatch<SetStateAction<WorkspaceProject | null>>;
  setActiveDocument: Dispatch<SetStateAction<WorkspaceDocument | null>>;
  setDocumentVersions: Dispatch<SetStateAction<DocumentVersion[]>>;
  refreshSecondary: () => Promise<void>;
  refreshArtifacts: () => Promise<void>;
  refreshInfra: () => Promise<void>;
  refreshPendingEdits: () => Promise<void>;
  activeConversationRef: RefObject<string | null>;
  activeProjectRef: RefObject<string | null>;
  activeDocumentRef: RefObject<string | null>;
};

export function createChatHandlers({
  bootstrap,
  selectedAgentId,
  setSelectedAgentId,
  selectedModel,
  selectedEffort,
  fast,
  thinking,
  attachedSkill,
  skillArgs,
  clearAttachedSkill,
  conversations,
  messages,
  draft,
  activeConversation,
  activeRun,
  setError,
  setNotice,
  setView,
  setSidebarOpen,
  setConversations,
  setActiveConversation,
  setMessages,
  setAgentCalls,
  setActiveRun,
  setRunStatus,
  setRunThinking,
  setBudgetPark,
  onScreenFlag,
  setDraft,
  setActiveProject,
  setActiveDocument,
  setDocumentVersions,
  refreshSecondary,
  refreshArtifacts,
  refreshInfra,
  refreshPendingEdits,
  activeConversationRef,
  activeProjectRef,
  activeDocumentRef,
}: ChatHandlerDeps) {
  async function selectConversation(id: string) {
    setActiveConversation(id);
    activeConversationRef.current = id;
    setSidebarOpen(false);
    setView("chat");
    setError("");
    try {
      setMessages(await api.listMessages(id));
    } catch (caught) {
      setError(describeError(caught, "Could not open conversation"));
    }
  }

  async function newConversation(spaceId = "") {
    try {
      const conversation = await api.createConversation(undefined, spaceId);
      setConversations((items) => [conversation, ...items]);
      setActiveConversation(conversation.id);
      activeConversationRef.current = conversation.id;
      setMessages([]);
      setView("chat");
      setSidebarOpen(false);
    } catch (caught) {
      setError(describeError(caught, "Could not create conversation"));
    }
  }

  /**
   * The active conversation, made if it does not exist yet. One copy, shared by
   * the send path and the approval-mode setter: both are things a person does
   * to a thread they can already see (the empty composer renders before the
   * row exists), so both must conjure the thread rather than silently no-op
   * against a null id — which is exactly what /plan picked on a fresh thread
   * used to do while `createConversation` was still in flight.
   */
  async function ensureConversation(): Promise<string> {
    if (activeConversation) return activeConversation;
    const created = await api.createConversation();
    setActiveConversation(created.id);
    activeConversationRef.current = created.id;
    setConversations((items) => [created, ...items]);
    return created.id;
  }

  const thread = createThreadHandlers({
    agentId: selectedAgentId || bootstrap?.default_agent_id,
    // Fast omits the effort so the backend's fast→low mapping wins; the
    // api-client drops an empty-string model or effort off the wire. An attached
    // skill and its args ride the same per-turn channel; `skillId` unset means
    // no skill, exactly today's behaviour.
    controls: {
      model: selectedModel,
      effort: fast ? "" : selectedEffort,
      fast,
      thinking,
      skillId: attachedSkill?.id,
      skillArgs,
    },
    messages,
    draft,
    activeConversation,
    activeRun,
    setError,
    setMessages,
    setAgentCalls,
    setActiveRun,
    setRunStatus,
    setRunThinking,
    setBudgetPark,
    setDraft,
    activeConversationRef,
    // The skill attachment is per-turn; drop it once the send is accepted.
    onSent: clearAttachedSkill,
    onScreenFlag,
    /** Typing into an empty rail starts a thread rather than refusing. */
    ensureConversation,
    onToolProposed: refreshSecondary,
    /**
     * A chat turn is the workspace's main event, so a finished one refreshes
     * essentially all of it: the tool calls and audit it produced, the
     * documents, folders, boards and dashboards it may have written, the
     * projects and connections, the pending edits it may have parked, and the
     * open project and document if the user is looking at one.
     */
    onAgentUnavailable: () => setSelectedAgentId(""),
    onRunSettled: async () => {
      setConversations(await api.listConversations());
      await refreshSecondary();
      await refreshArtifacts().catch(() => undefined);
      await refreshInfra().catch(() => undefined);
      await refreshPendingEdits().catch(() => undefined);
      const openProjectId = activeProjectRef.current;
      if (openProjectId) {
        setActiveProject(await api.getProject(openProjectId).catch(() => null));
      }
      const open = activeDocumentRef.current;
      if (open) {
        setActiveDocument(await api.getDocument(open).catch(() => null));
        setDocumentVersions(await api.listDocumentVersions(open).catch(() => []));
      }
    },
  });

  /**
   * Branch a new personal thread from everything said up to one message, and
   * land in it. The fork is just another conversation — the send/stream loop
   * needs no special case — so all this does is prepend the server's row,
   * make it active (state and ref together, like `selectConversation`), and
   * load its copied transcript.
   */
  async function forkThread(messageId: string) {
    if (!activeConversation) return;
    setError("");
    try {
      const fork = await api.forkConversation(activeConversation, messageId);
      setConversations((items) => [fork, ...items]);
      setActiveConversation(fork.id);
      activeConversationRef.current = fork.id;
      setView("chat");
      setSidebarOpen(false);
      setMessages(await api.listMessages(fork.id));
    } catch (caught) {
      setError(describeError(caught, "Could not fork the thread"));
    }
  }

  /**
   * Revert a finished run's writes from its recorded checkpoints.
   *
   * Confirmed first — this rewrites documents and boards — and honest after:
   * something is said only when something could NOT be reverted, because "it
   * worked" needs no banner and "it half-worked" is exactly what must not pass
   * silently. Which banner is the point. A restore the clobber guard refused
   * protected the user's own later edits and can be retried; dropping it in
   * the red toast beside a crashed restore would make a working safeguard read
   * as a failure, so only `failed` outcomes go there. The neutral toast is
   * pinned open for this one: unlike a presentation flip, an undo's skipped
   * half is a thing to act on, not to glimpse. The same off-screen-work
   * refreshes a settled run triggers run afterwards, since an undo changes the
   * same surfaces a run does.
   */
  async function undoRun(runId: string) {
    if (!window.confirm(UNDO_CONFIRM)) return;
    setError("");
    try {
      const result = await api.revertRun(runId);
      const outcome = summarizeUndo(result);
      if (outcome.text) {
        if (outcome.failed) setError(outcome.text);
        else setNotice({ text: outcome.text, at: Date.now(), sticky: true });
      }
      await refreshSecondary();
      await refreshArtifacts().catch(() => undefined);
      await refreshPendingEdits().catch(() => undefined);
      const open = activeDocumentRef.current;
      if (open) {
        setActiveDocument(await api.getDocument(open).catch(() => null));
        setDocumentVersions(await api.listDocumentVersions(open).catch(() => []));
      }
    } catch (caught) {
      setError(describeError(caught, "Could not undo the run"));
    }
  }

  async function removeConversation(
    conversation: Conversation,
    event?: MouseEvent,
  ) {
    event?.stopPropagation();
    if (!window.confirm(`Delete “${conversation.title}”?`)) return;
    setError("");
    try {
      if (activeConversation === conversation.id && activeRun) {
        try {
          await api.cancelRun(activeRun);
        } catch {
          // Continue deleting even if cancel fails for a finished run.
        }
      }
      await api.deleteConversation(conversation.id);
      const remaining = conversations.filter((item) => item.id !== conversation.id);
      setConversations(remaining);
      if (activeConversation === conversation.id) {
        setActiveRun(null);
        setRunStatus("");
        if (remaining[0]) {
          await selectConversation(remaining[0].id);
        } else {
          setActiveConversation(null);
          activeConversationRef.current = null;
          setMessages([]);
        }
      }
      await refreshSecondary();
    } catch (caught) {
      setError(describeError(caught, "Could not delete chat"));
    }
  }

  /**
   * Change how much this thread asks before acting.
   *
   * The updated Conversation replaces the one in the rail's list, so the mode
   * has exactly one home in the client: the picker, the indicator and the
   * badge all read the same row, and switching threads shows the other
   * thread's answer rather than a control that remembers the last one you set.
   * The server re-reads the mode per tool call, so turning the bypass off here
   * parks the very next write even mid-turn.
   */
  async function setApprovalMode(mode: ApprovalMode) {
    if (!activeConversation) return;
    setError("");
    try {
      const updated = await api.setApprovalMode(activeConversation, mode);
      setConversations((items) =>
        items.map((item) => (item.id === updated.id ? updated : item)),
      );
    } catch (caught) {
      setError(describeError(caught, "Could not change the approval mode"));
    }
  }

  return {
    selectConversation,
    newConversation,
    forkThread,
    undoRun,
    removeConversation,
    setApprovalMode,
    decideAgentCall: thread.decideAgentCall,
    cancelActiveRun: thread.cancelActiveRun,
    regenerate: thread.regenerate,
    editMessage: thread.editMessage,
    submitPrompt: thread.submitPrompt,
  };
}
