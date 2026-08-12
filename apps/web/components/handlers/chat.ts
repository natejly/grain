"use client";

import type {
  AgentToolCall,
  ApprovalMode,
  Bootstrap,
  Conversation,
  DocumentVersion,
  Message,
  WorkspaceDocument,
  WorkspaceProject,
} from "@workspace/api-client";
import type { Dispatch, MouseEvent, RefObject, SetStateAction } from "react";
import { api } from "../api";
import type { BudgetPark } from "../views/budget-format";
import { describeError, type View } from "../views/shared";
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
  conversations: Conversation[];
  messages: Message[];
  draft: string;
  activeConversation: string | null;
  activeRun: string | null;
  setError: Dispatch<SetStateAction<string>>;
  setView: Dispatch<SetStateAction<View>>;
  setSidebarOpen: Dispatch<SetStateAction<boolean>>;
  setConversations: Dispatch<SetStateAction<Conversation[]>>;
  setActiveConversation: Dispatch<SetStateAction<string | null>>;
  setMessages: Dispatch<SetStateAction<Message[]>>;
  setAgentCalls: Dispatch<SetStateAction<AgentToolCall[]>>;
  setActiveRun: Dispatch<SetStateAction<string | null>>;
  setRunStatus: Dispatch<SetStateAction<string>>;
  setBudgetPark: Dispatch<SetStateAction<BudgetPark | null>>;
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
  conversations,
  messages,
  draft,
  activeConversation,
  activeRun,
  setError,
  setView,
  setSidebarOpen,
  setConversations,
  setActiveConversation,
  setMessages,
  setAgentCalls,
  setActiveRun,
  setRunStatus,
  setBudgetPark,
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

  async function newConversation() {
    try {
      const conversation = await api.createConversation();
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

  const thread = createThreadHandlers({
    agentId: bootstrap?.default_agent_id,
    messages,
    draft,
    activeConversation,
    activeRun,
    setError,
    setMessages,
    setAgentCalls,
    setActiveRun,
    setRunStatus,
    setBudgetPark,
    setDraft,
    activeConversationRef,
    /** Typing into an empty rail starts a thread rather than refusing. */
    ensureConversation: async () => {
      if (activeConversation) return activeConversation;
      const created = await api.createConversation();
      setActiveConversation(created.id);
      activeConversationRef.current = created.id;
      setConversations((items) => [created, ...items]);
      return created.id;
    },
    onToolProposed: refreshSecondary,
    /**
     * A chat turn is the workspace's main event, so a finished one refreshes
     * essentially all of it: the tool calls and audit it produced, the
     * documents, folders, boards and dashboards it may have written, the
     * projects and connections, the pending edits it may have parked, and the
     * open project and document if the user is looking at one.
     */
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
    removeConversation,
    setApprovalMode,
    decideAgentCall: thread.decideAgentCall,
    cancelActiveRun: thread.cancelActiveRun,
    regenerate: thread.regenerate,
    submitPrompt: thread.submitPrompt,
  };
}
