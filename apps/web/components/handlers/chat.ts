"use client";

import type {
  AgentToolCall,
  Bootstrap,
  Citation,
  CitationCheck,
  Conversation,
  DocumentVersion,
  Message,
  ToolArtifact,
  WorkspaceDocument,
  WorkspaceProject,
} from "@workspace/api-client";
import type {
  Dispatch,
  FormEvent,
  MouseEvent,
  RefObject,
  SetStateAction,
} from "react";
import { api } from "../api";
import { readBudgetPark, type BudgetPark } from "../views/budget-format";
import { readCitationCheck } from "../views/citation-format";
import { describeError, type View } from "../views/shared";

/**
 * A tool failure, named.
 *
 * The API reports the same sentence twice — once on `tool.failed` with the
 * tool's name and once on `run.failed` without it — so the name is prepended
 * here rather than being lost to whichever event happened to land last. An
 * unnamed tool failure tells a user nothing they can act on: the whole question
 * is *which* of the things they approved went wrong.
 */
function toolFailure(tool: string, error: string): string {
  const detail = error || "the call failed";
  return tool ? `${tool} failed: ${detail}` : detail;
}

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
   * Merge a tool event into the card list. Events arrive in stages (proposed →
   * running → completed) and each carries only its own fields, so later stages
   * patch the existing row rather than replacing it.
   */
  function upsertAgentCall(
    runId: string,
    data: Record<string, unknown>,
    status: string,
  ) {
    const id = String(data.tool_call_id || "");
    if (!id) return;
    setAgentCalls((items) => {
      const index = items.findIndex((item) => item.id === id);
      const patch = {
        status,
        ...(data.tool_name ? { name: String(data.tool_name) } : {}),
        ...(data.arguments ? { arguments_json: String(data.arguments) } : {}),
        ...(data.preview && status !== "proposed"
          ? { result_preview: String(data.preview) }
          : {}),
        ...(data.preview && status === "proposed"
          ? { proposal_preview: String(data.preview) }
          : {}),
        // Only tool.completed carries these, and only for a tool that produced
        // files. An absent key must leave what is already on the card alone —
        // patching it to [] would erase a chart on the next event.
        ...(Array.isArray(data.artifacts)
          ? { artifacts: data.artifacts as ToolArtifact[] }
          : {}),
      };
      if (index >= 0) {
        return items.map((item, position) =>
          position === index ? { ...item, ...patch } : item,
        );
      }
      return [
        ...items,
        {
          id,
          run_id: runId,
          conversation_id: activeConversationRef.current || "",
          name: String(data.tool_name || ""),
          arguments_json: String(data.arguments || "{}"),
          proposal_preview:
            status === "proposed" ? String(data.preview || "") : "",
          status,
          result_preview: String(data.preview || ""),
          error: "",
          latency_ms: 0,
          artifacts: Array.isArray(data.artifacts)
            ? (data.artifacts as ToolArtifact[])
            : [],
          created_at: new Date().toISOString(),
        },
      ];
    });
  }

  async function decideAgentCall(
    call: AgentToolCall,
    decision: "approved" | "denied",
    remember: boolean,
  ) {
    setError("");
    try {
      await api.decideAgentToolCall(call.id, decision, remember);
      setAgentCalls((items) =>
        items.map((item) => (item.id === call.id ? { ...item, status: decision } : item)),
      );
      setRunStatus(decision === "approved" ? "Resuming" : "Continuing without the tool");
    } catch (caught) {
      setError(describeError(caught, "Could not record that decision"));
    }
  }

  async function cancelActiveRun() {
    if (!activeRun) return;
    try {
      await api.cancelRun(activeRun);
      setRunStatus("Stopping");
    } catch (caught) {
      setError(describeError(caught, "Could not stop the run"));
    }
  }

  /** Re-ask the most recent user prompt as a fresh turn. */
  async function regenerate() {
    const lastUser = [...messages].reverse().find((item) => item.role === "user");
    if (!lastUser || activeRun || !activeConversation) return;
    setError("");
    try {
      const response = await api.sendMessage(
        activeConversation,
        lastUser.content,
        bootstrap?.default_agent_id,
      );
      setMessages((items) =>
        items.some((item) => item.id === response.message.id)
          ? items
          : [...items, response.message],
      );
      void followRun(response.run.id, activeConversation);
    } catch (caught) {
      setError(describeError(caught, "Could not regenerate"));
    }
  }

  async function followRun(runId: string, conversationId: string) {
    const temporaryId = `streaming-${runId}`;
    setActiveRun(runId);
    setRunStatus("Starting");
    setBudgetPark(null);
    /**
     * The citation validator's verdict, which arrives just before the message
     * it judges. Held in a local rather than in state because it belongs to one
     * message and one run: `message.completed` reads it a few lines later and
     * builds the message with it, so the badge is on screen from the moment the
     * answer is, and the refetch that follows carries the same field from the
     * server. One field, two arrival paths, no reconciliation.
     */
    let citationReport: CitationCheck | null = null;
    /**
     * Which tool blew up, if one did. `run.failed` follows `tool.failed` with
     * the same error and no name, so the last-writer-wins ordering would throw
     * away the only useful half of the message.
     */
    let failedTool = "";
    try {
      for await (const event of api.streamRun(runId)) {
        if (event.event === "run.started") {
          // Also the *release*: `resume_run_after_budget` emits run.started on
          // the same run, on this same still-open stream, the moment an owner
          // raises the ceiling. Clearing the park here is what turns the hold
          // card back into a running turn without anybody reloading.
          setBudgetPark(null);
          setRunStatus("Searching sources");
        }
        /**
         * The spend ceiling stopped the turn before its next model call.
         *
         * A park is not a failure and not an approval: there is no
         * `AgentToolCall`, so no card is coming, and the run stays open and
         * resumable. The status line is cleared because the thinking dots would
         * claim work is happening — the panel this raises says what is actually
         * true, which is that nothing will happen until the ceiling moves.
         */
        if (event.event === "run.waiting_for_budget") {
          const park = readBudgetPark(event.data);
          if (park) {
            setBudgetPark(park);
            setRunStatus("");
          }
        }
        if (event.event === "retrieval.completed") {
          const count = Number(event.data.count || 0);
          setRunStatus(count ? `Using ${count} source passages` : "No matching source");
        }
        if (event.event === "memory.recalled") {
          const count = Number(event.data.count || 0);
          if (count > 0) {
            setRunStatus(`Recalling ${count} ${count === 1 ? "memory" : "memories"}`);
          }
        }
        if (event.event === "message.delta") {
          const delta = String(event.data.delta || "");
          setMessages((items) => {
            const existing = items.findIndex((item) => item.id === temporaryId);
            if (existing >= 0) {
              return items.map((item, index) =>
                index === existing ? { ...item, content: item.content + delta } : item,
              );
            }
            return [
              ...items,
              {
                id: temporaryId,
                run_id: runId,
                role: "assistant",
                content: delta,
                citations: [],
                // Not yet checked: the validator runs on the finished answer,
                // and a verdict on half a sentence would be a lie either way.
                citation_report: null,
                created_at: new Date().toISOString(),
              },
            ];
          });
        }
        if (event.event === "tool.proposed") {
          setRunStatus("Waiting for your approval");
          // The legacy HTTP tool carries request_url and is decided through a
          // different endpoint; the agent loop's calls have no URL.
          if (event.data.request_url === undefined) {
            upsertAgentCall(runId, event.data, "proposed");
          }
          await refreshSecondary();
        }
        if (event.event === "tool.started") {
          const name = String(event.data.tool_name || "");
          setRunStatus(name ? `Running ${name}` : "Running approved read-only tool");
          upsertAgentCall(runId, event.data, "running");
        }
        if (event.event === "tool.completed") {
          upsertAgentCall(runId, event.data, String(event.data.status || "succeeded"));
        }
        /**
         * A tool threw. The approval-gated HTTP tool fails here rather than
         * returning a result, and this is the only event that says *which* tool
         * — `run.failed` arrives next carrying the same message with the name
         * stripped out, which is how "the run failed" became the entire report
         * on a tool the user had approved by name a second earlier.
         */
        if (event.event === "tool.failed") {
          failedTool = String(event.data.tool_name || "");
          upsertAgentCall(runId, event.data, "failed");
          setError(toolFailure(failedTool, String(event.data.error || "")));
        }
        /**
         * The citation contract, checked. This is the product's central
         * technical claim and it was reported to an audit row nobody reads.
         */
        if (event.event === "run.citations") {
          citationReport = readCitationCheck(event.data);
        }
        if (event.event === "message.completed") {
          const completed: Message = {
            id: String(event.data.message_id),
            run_id: runId,
            role: "assistant",
            content: String(event.data.content || ""),
            citations: (event.data.citations || []) as Citation[],
            citation_report: citationReport,
            created_at: new Date().toISOString(),
          };
          setMessages((items) => {
            const withoutTemporary = items.filter((item) => item.id !== temporaryId);
            if (withoutTemporary.some((item) => item.id === completed.id)) {
              return withoutTemporary;
            }
            return [...withoutTemporary, completed];
          });
        }
        if (event.event === "run.failed") {
          const error = String(event.data.error || "The run failed");
          setError(failedTool ? toolFailure(failedTool, error) : error);
        }
      }
      if (activeConversationRef.current === conversationId) {
        setMessages(await api.listMessages(conversationId));
      }
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
    } catch (caught) {
      setError(describeError(caught, "The event stream disconnected"));
    } finally {
      setActiveRun((current) => (current === runId ? null : current));
      setRunStatus("");
      // The stream only ends once the run is terminal, and a parked run is not
      // terminal — so reaching here means this run is finished, cancelled or
      // disconnected, and a hold card for it would outlive the hold.
      setBudgetPark(null);
    }
  }

  async function submitPrompt(event?: FormEvent) {
    event?.preventDefault();
    const content = draft.trim();
    if (!content || activeRun) return;
    setDraft("");
    setError("");
    try {
      let conversationId = activeConversation;
      if (!conversationId) {
        const created = await api.createConversation();
        conversationId = created.id;
        setActiveConversation(created.id);
        activeConversationRef.current = created.id;
        setConversations((items) => [created, ...items]);
      }
      const response = await api.sendMessage(
        conversationId,
        content,
        bootstrap?.default_agent_id,
      );
      setMessages((items) => {
        const existing = items.some((item) => item.id === response.message.id);
        return existing ? items : [...items, response.message];
      });
      void followRun(response.run.id, conversationId);
    } catch (caught) {
      setDraft(content);
      setError(describeError(caught, "Could not send message"));
    }
  }

  return {
    selectConversation,
    newConversation,
    removeConversation,
    decideAgentCall,
    cancelActiveRun,
    regenerate,
    submitPrompt,
  };
}
