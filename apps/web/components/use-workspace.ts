"use client";

import type {
  AgentToolCall,
  AuditEvent,
  Board,
  Bootstrap,
  Conversation,
  Dataset,
  DbConnection,
  DocumentSummary,
  DocumentVersion,
  GeneratedApp,
  IntegrationProvider,
  KnowledgeGraph,
  McpServer,
  MemoryItem,
  Message,
  PendingDocumentEdit,
  ProjectSummary,
  ProvenanceChunk,
  Source,
  ToolCall,
  WorkspaceDocument,
  WorkspaceProject,
} from "@workspace/api-client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import { createBoardHandlers } from "./handlers/boards";
import { createChatHandlers } from "./handlers/chat";
import { createDashboardHandlers } from "./handlers/dashboards";
import { createDocumentHandlers } from "./handlers/documents";
import { createGraphHandlers } from "./handlers/graph";
import { createInfraHandlers } from "./handlers/infra";
import { createIntegrationHandlers } from "./handlers/integrations";
import { createMcpHandlers } from "./handlers/mcp";
import { createSourceHandlers } from "./handlers/sources";
import type { BudgetPark } from "./views/budget-format";
import { baseName, describeError, isTabular, type View } from "./views/shared";

/**
 * Every piece of workspace state and the actions over it. The shell renders the
 * result; the hooks below run in the order they always have.
 */
export function useWorkspace() {
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeConversation, setActiveConversation] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [sources, setSources] = useState<Source[]>([]);
  const [toolCalls, setToolCalls] = useState<ToolCall[]>([]);
  const [agentCalls, setAgentCalls] = useState<AgentToolCall[]>([]);
  const [auditEvents, setAuditEvents] = useState<AuditEvent[]>([]);
  const [graph, setGraph] = useState<KnowledgeGraph | null>(null);
  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [apps, setApps] = useState<GeneratedApp[]>([]);
  const [integrations, setIntegrations] = useState<IntegrationProvider[]>([]);
  const [mcpServers, setMcpServers] = useState<McpServer[]>([]);
  const [documents, setDocuments] = useState<DocumentSummary[]>([]);
  const [activeDocument, setActiveDocument] = useState<WorkspaceDocument | null>(null);
  const [documentVersions, setDocumentVersions] = useState<DocumentVersion[]>([]);
  const [boards, setBoards] = useState<Board[]>([]);
  const [pendingEdits, setPendingEdits] = useState<PendingDocumentEdit[]>([]);
  const [dbConnections, setDbConnections] = useState<DbConnection[]>([]);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [activeProject, setActiveProject] = useState<WorkspaceProject | null>(null);
  const [view, setView] = useState<View>("chat");
  const [draft, setDraft] = useState("");
  const [activeRun, setActiveRun] = useState<string | null>(null);
  const [runStatus, setRunStatus] = useState("");
  /**
   * The spend ceiling that stopped the turn being streamed, if one did.
   *
   * Held here rather than in the chat view because it is run state, not view
   * state: the run is still open and still resumable, and the card has to
   * survive every re-render until the run either resumes or is cancelled.
   */
  const [budgetPark, setBudgetPark] = useState<BudgetPark | null>(null);
  const [provenance, setProvenance] = useState<ProvenanceChunk | null>(null);
  const [loadingProvenance, setLoadingProvenance] = useState(false);
  const [editing, setEditing] = useState<string | "new" | null>(null);
  const [uploading, setUploading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [error, setError] = useState("");
  const endRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const activeConversationRef = useRef<string | null>(null);
  const conversationsRef = useRef<Conversation[]>([]);
  const activeDocumentRef = useRef<string | null>(null);
  const activeProjectRef = useRef<string | null>(null);
  const datasetAttempts = useRef(new Set<string>());

  const pendingApprovals = useMemo(
    () => toolCalls.filter((call) => call.status === "proposed"),
    [toolCalls],
  );

  const dashboards = useMemo(
    () => apps.filter((app) => app.app_type === "code"),
    [apps],
  );

  const refreshSecondary = useCallback(async () => {
    const [nextSources, nextTools, nextAgentCalls, nextAudit] = await Promise.all([
      api.listSources(),
      api.listToolCalls(),
      api.listAgentToolCalls(),
      api.listAuditEvents(),
    ]);
    setSources(nextSources);
    setToolCalls(nextTools);
    setAgentCalls(nextAgentCalls);
    setAuditEvents(nextAudit);
  }, []);

  const refreshExpansion = useCallback(async () => {
    const [
      nextGraph,
      nextMemories,
      nextDatasets,
      nextApps,
      nextIntegrations,
      nextMcp,
    ] = await Promise.all([
      api.getGraph(),
      api.listMemory(),
      api.listDatasets(),
      api.listApps(),
      api.listIntegrations(),
      api.listMcpServers(),
    ]);
    setGraph(nextGraph);
    setMemories(nextMemories);
    setDatasets(nextDatasets);
    setApps(nextApps);
    setIntegrations(nextIntegrations);
    setMcpServers(nextMcp);
  }, []);

  const refreshArtifacts = useCallback(async () => {
    const [nextDocuments, nextBoards] = await Promise.all([
      api.listDocuments(),
      api.listBoards(),
    ]);
    setDocuments(nextDocuments);
    setBoards(nextBoards);
  }, []);

  const refreshInfra = useCallback(async () => {
    const [nextConnections, nextProjects] = await Promise.all([
      api.listDbConnections(),
      api.listProjects(),
    ]);
    setDbConnections(nextConnections);
    setProjects(nextProjects);
  }, []);

  const refreshPendingEdits = useCallback(async () => {
    setPendingEdits(await api.listPendingDocumentEdits());
  }, []);

  /**
   * Catch up after something changed the workspace without going through chat.
   *
   * A workflow run is the case this exists for: it approves a write, creates a
   * document, and resolves a pending edit, none of which the shell's lists hear
   * about — a chat turn refreshes them when its run ends, and a workflow run
   * has no such subscriber. Without this the Documents view keeps showing the
   * list it fetched at page load, and Chat keeps offering an approval that has
   * already been answered.
   */
  const refreshOffScreenWork = useCallback(async () => {
    await Promise.all([refreshSecondary(), refreshArtifacts(), refreshPendingEdits()]);
  }, [refreshSecondary, refreshArtifacts, refreshPendingEdits]);

  const loadWorkspace = useCallback(async () => {
    // The list this load is allowed to overwrite. Anything that appears in the
    // sidebar *after* this snapshot was taken was created by the user while the
    // load was in flight, and a response fetched before it existed must not
    // erase it — "New thread" clicked on a still-loading workspace used to lose
    // the thread it just made.
    const knownAtStart = new Set(conversationsRef.current.map((item) => item.id));
    try {
      const [
        boot,
        chats,
        nextSources,
        nextTools,
        nextAgentCalls,
        nextAudit,
        nextGraph,
        nextMemories,
        nextDatasets,
        nextApps,
        nextIntegrations,
        nextMcp,
      ] = await Promise.all([
        api.bootstrap(),
        api.listConversations(),
        api.listSources(),
        api.listToolCalls(),
        api.listAgentToolCalls(),
        api.listAuditEvents(),
        api.getGraph(),
        api.listMemory(),
        api.listDatasets(),
        api.listApps(),
        api.listIntegrations(),
        api.listMcpServers(),
      ]);
      setBootstrap(boot);
      setConversations((current) => {
        const listed = new Set(chats.map((item) => item.id));
        const createdDuringLoad = current.filter(
          (item) => !listed.has(item.id) && !knownAtStart.has(item.id),
        );
        return [...createdDuringLoad, ...chats];
      });
      setSources(nextSources);
      setToolCalls(nextTools);
      setAgentCalls(nextAgentCalls);
      setAuditEvents(nextAudit);
      setGraph(nextGraph);
      setMemories(nextMemories);
      setDatasets(nextDatasets);
      setApps(nextApps);
      setIntegrations(nextIntegrations);
      setMcpServers(nextMcp);
      setError("");
      if (chats[0] && !activeConversationRef.current) {
        setActiveConversation(chats[0].id);
        activeConversationRef.current = chats[0].id;
        setMessages(await api.listMessages(chats[0].id));
      }
    } catch (caught) {
      setError(describeError(caught, "Could not load workspace"));
    }
  }, []);

  useEffect(() => {
    void loadWorkspace();
  }, [loadWorkspace]);

  useEffect(() => {
    void refreshArtifacts().catch(() => undefined);
  }, [refreshArtifacts]);

  useEffect(() => {
    void refreshInfra().catch(() => undefined);
  }, [refreshInfra]);

  useEffect(() => {
    void refreshPendingEdits().catch(() => undefined);
  }, [refreshPendingEdits]);

  useEffect(() => {
    conversationsRef.current = conversations;
  }, [conversations]);

  useEffect(() => {
    activeDocumentRef.current = activeDocument?.id ?? null;
  }, [activeDocument]);

  useEffect(() => {
    activeProjectRef.current = activeProject?.id ?? null;
  }, [activeProject]);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const connected = params.get("connected");
    const failed = params.get("integration_error");
    if (!connected && !failed) return;
    window.history.replaceState(null, "", window.location.pathname);
    setView("integrations");
    if (failed) setError(`Could not connect ${failed}. Try again.`);
  }, []);

  useEffect(() => {
    if (!sources.some((source) => ["queued", "processing"].includes(source.status))) {
      return;
    }
    const timer = window.setInterval(() => {
      void api
        .listSources()
        .then(setSources)
        .catch(() => undefined);
    }, 1500);
    return () => window.clearInterval(timer);
  }, [sources]);

  // A ready CSV/JSON source becomes a queryable dataset on its own, so
  // dashboards can bind to it without a separate builder step.
  useEffect(() => {
    const pending = sources.filter(
      (source) =>
        source.status === "ready" &&
        isTabular(source.filename) &&
        !datasets.some((dataset) => dataset.source_id === source.id) &&
        !datasetAttempts.current.has(source.id),
    );
    if (pending.length === 0) return;
    pending.forEach((source) => datasetAttempts.current.add(source.id));
    void (async () => {
      for (const source of pending) {
        try {
          const created = await api.createDataset(
            baseName(source.filename),
            source.id,
          );
          setDatasets((items) =>
            items.some((item) => item.id === created.id)
              ? items
              : [created, ...items],
          );
        } catch {
          // The source is still searchable in chat; it just has no dataset.
        }
      }
    })();
  }, [sources, datasets]);

  useEffect(() => {
    if (!graph || !["queued", "building"].includes(graph.status)) return;
    const timer = window.setInterval(() => {
      void api
        .getGraph()
        .then(setGraph)
        .catch(() => undefined);
    }, 500);
    return () => window.clearInterval(timer);
  }, [graph]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [messages, runStatus]);

  const documentHandlers = createDocumentHandlers({
    setError,
    setDocuments,
    setActiveDocument,
    setDocumentVersions,
    setPendingEdits,
    refreshArtifacts,
    activeDocumentRef,
  });

  const boardHandlers = createBoardHandlers({ setError, setBoards });

  const infraHandlers = createInfraHandlers({
    setError,
    setDbConnections,
    setProjects,
    setActiveProject,
  });

  const mcpHandlers = createMcpHandlers({ setError, setMcpServers });

  const chatHandlers = createChatHandlers({
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
  });

  const sourceHandlers = createSourceHandlers({
    setError,
    setView,
    setUploading,
    setDragging,
    refreshSecondary,
    refreshExpansion,
    fileInputRef,
  });

  const graphHandlers = createGraphHandlers({
    setError,
    setGraph,
    setMemories,
    setProvenance,
    setLoadingProvenance,
    refreshSecondary,
  });

  const dashboardHandlers = createDashboardHandlers({
    setError,
    setApps,
    refreshSecondary,
  });

  const integrationHandlers = createIntegrationHandlers({
    setError,
    setIntegrations,
    refreshSecondary,
    refreshExpansion,
  });

  return {
    bootstrap,
    conversations,
    activeConversation,
    messages,
    sources,
    toolCalls,
    agentCalls,
    auditEvents,
    graph,
    memories,
    datasets,
    apps,
    integrations,
    mcpServers,
    documents,
    activeDocument,
    documentVersions,
    boards,
    pendingEdits,
    dbConnections,
    projects,
    activeProject,
    view,
    setView,
    draft,
    setDraft,
    activeRun,
    runStatus,
    budgetPark,
    provenance,
    setProvenance,
    loadingProvenance,
    editing,
    setEditing,
    uploading,
    dragging,
    setDragging,
    sidebarOpen,
    setSidebarOpen,
    error,
    setError,
    endRef,
    fileInputRef,
    pendingApprovals,
    dashboards,
    loadWorkspace,
    refreshOffScreenWork,
    ...documentHandlers,
    ...boardHandlers,
    ...infraHandlers,
    ...mcpHandlers,
    ...chatHandlers,
    ...sourceHandlers,
    ...graphHandlers,
    ...dashboardHandlers,
    ...integrationHandlers,
  };
}
