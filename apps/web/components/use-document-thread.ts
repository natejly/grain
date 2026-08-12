"use client";

import type { AgentToolCall, Message } from "@workspace/api-client";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { createThreadHandlers } from "./handlers/thread";
import type { BudgetPark } from "./views/budget-format";
import { describeError } from "./views/shared";

export type DocumentThreadDeps = {
  /** The open document, or "" when none is. */
  documentId: string;
  /** The agent to run as, from bootstrap. */
  agentId?: string;
  /** Re-read the open document and its versions after a write lands. */
  reloadDocument: () => Promise<void>;
  /** Re-read `GET /api/documents-pending`, which the inline review renders. */
  refreshPendingEdits: () => Promise<void>;
};

/**
 * The chat thread that belongs to one document.
 *
 * Its own state, not the shell's: the rail's thread and this one can both be
 * mid-run, and sharing `activeRun` between them would have a document edit's
 * spinner stop the composer in Chat. What it does *not* have its own copy of is
 * the turn — that is `createThreadHandlers`, the same code the rail runs.
 *
 * The thread is fetched rather than created: `POST /api/documents/{id}/
 * conversation` is get-or-create keyed on the document, so a remount, a second
 * tab and a StrictMode double-invoke all land on the same conversation. That is
 * why there is no "have I already made one" flag here — there is nothing to
 * guard, which is the point of putting the key on the server side.
 */
export function useDocumentThread({
  documentId,
  agentId,
  reloadDocument,
  refreshPendingEdits,
}: DocumentThreadDeps) {
  const [conversationId, setConversationId] = useState<string | null>(null);
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
   * The document the panel is loading for. A user who clicks a second file
   * while the first thread is still in flight must not get the first file's
   * messages: the response is discarded when this no longer names its document.
   */
  const loadingFor = useRef("");

  useEffect(() => {
    conversationRef.current = conversationId;
  }, [conversationId]);

  useEffect(() => {
    loadingFor.current = documentId;
    setConversationId(null);
    conversationRef.current = null;
    setMessages([]);
    setAgentCalls([]);
    setDraft("");
    setError("");
    // The run belongs to the file we just left. Leaving it set disables the new
    // file's composer, turns Send into a Stop wired to somebody else's run, and
    // leaves a budget hold on screen for a document that has none — the panel
    // would be reporting the previous file's turn as this one's.
    setActiveRun(null);
    setRunStatus("");
    setBudgetPark(null);
    if (!documentId) return;
    void (async () => {
      try {
        const conversation = await api.documentConversation(documentId);
        const history = await api.listMessages(conversation.id);
        if (loadingFor.current !== documentId) return;
        setConversationId(conversation.id);
        conversationRef.current = conversation.id;
        setMessages(history);
      } catch (caught) {
        if (loadingFor.current !== documentId) return;
        setError(describeError(caught, "Could not open the chat for this document"));
      }
    })();
  }, [documentId]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [messages, runStatus]);

  const onRunSettled = useCallback(async () => {
    // Order matters: the pending list is what the inline review renders, and a
    // decided edit has to stop being offered before the document it changed is
    // re-read, or the review flashes back over the text it already applied.
    await refreshPendingEdits().catch(() => undefined);
    await reloadDocument().catch(() => undefined);
    setAgentCalls(await api.listAgentToolCalls().catch(() => []));
  }, [refreshPendingEdits, reloadDocument]);

  const thread = createThreadHandlers({
    agentId,
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
    // Never creates: the thread is the document's, and a panel that has not
    // resolved it yet has nothing to send into.
    ensureConversation: async () => {
      if (!conversationId) throw new Error("This document has no chat thread yet");
      return conversationId;
    },
    // The parked edit is the whole point of this panel. Without this refresh
    // the hunks stay unreviewable until something else happens to refetch.
    onToolProposed: refreshPendingEdits,
    onRunSettled,
  });

  return {
    conversationId,
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
