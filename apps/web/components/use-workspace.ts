"use client";

import type {
  AgentToolCall,
  AuditEvent,
  Board,
  Bootstrap,
  Conversation,
  Dashboard,
  DashboardPin,
  DashboardTemplate,
  Dataset,
  DbConnection,
  DocumentSummary,
  DocumentVersion,
  Folder,
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
  WorkspaceDocument,
  WorkspaceProject,
} from "@workspace/api-client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import { createBoardHandlers } from "./handlers/boards";
import { createChatHandlers } from "./handlers/chat";
import { createDashboardHandlers } from "./handlers/dashboards";
import { createDocumentHandlers } from "./handlers/documents";
import { createFolderHandlers } from "./handlers/folders";
import { createGraphHandlers } from "./handlers/graph";
import { createInfraHandlers } from "./handlers/infra";
import { createIntegrationHandlers } from "./handlers/integrations";
import { createMcpHandlers } from "./handlers/mcp";
import { createSourceHandlers } from "./handlers/sources";
import type { BudgetPark } from "./views/budget-format";
import type { DashboardResultState } from "./views/dashboard-grid";
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
  const [agentCalls, setAgentCalls] = useState<AgentToolCall[]>([]);
  const [auditEvents, setAuditEvents] = useState<AuditEvent[]>([]);
  const [graph, setGraph] = useState<KnowledgeGraph | null>(null);
  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [apps, setApps] = useState<GeneratedApp[]>([]);
  const [integrations, setIntegrations] = useState<IntegrationProvider[]>([]);
  const [mcpServers, setMcpServers] = useState<McpServer[]>([]);
  const [documents, setDocuments] = useState<DocumentSummary[]>([]);
  const [folders, setFolders] = useState<Folder[]>([]);
  const [activeDocument, setActiveDocument] = useState<WorkspaceDocument | null>(null);
  const [documentVersions, setDocumentVersions] = useState<DocumentVersion[]>([]);
  const [boards, setBoards] = useState<Board[]>([]);
  const [pendingEdits, setPendingEdits] = useState<PendingDocumentEdit[]>([]);
  const [dbConnections, setDbConnections] = useState<DbConnection[]>([]);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [activeProject, setActiveProject] = useState<WorkspaceProject | null>(null);
  const [view, setView] = useState<View>("chat");
  const [draft, setDraft] = useState("");
  /**
   * Per-turn model/effort/fast overrides for the chat composer. They start
   * empty and are seeded from the bootstrap once it arrives (below), so the
   * selectors show the deployment's real defaults rather than a guess; a user's
   * own choice sticks because the seed only fills a value still unset.
   */
  const [selectedModel, setSelectedModel] = useState("");
  const [selectedEffort, setSelectedEffort] = useState("");
  const [fast, setFast] = useState(false);
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

  /**
   * The dashboards the agent authors, and this user's curation of them.
   *
   * `dashboardResults` is keyed by dashboard id and lives here rather than in
   * the grid so that navigating away and back does not re-run six queries; the
   * ref beside it is what stops two tiles mounted in the same commit both
   * deciding nothing has been fetched yet.
   */
  const [dashboards, setDashboards] = useState<Dashboard[]>([]);
  const [dashboardTemplates, setDashboardTemplates] = useState<DashboardTemplate[]>([]);
  const [dashboardPins, setDashboardPins] = useState<DashboardPin[]>([]);
  const [dashboardResults, setDashboardResults] = useState<
    Record<string, DashboardResultState>
  >({});
  const requestedDashboards = useRef(new Set<string>());
  /** A dashboard the rail asked for; the grid scrolls to it and outlines it. */
  const [focusedDashboard, setFocusedDashboard] = useState<string | null>(null);

  /**
   * Drives the Activity badge, so it has to count the approvals that actually
   * happen: `AgentToolCall`s parked by the agent loop. It counted legacy
   * `ToolCall`s until now, and nothing outside the dev seed creates the `Tool`
   * rows those hang off — so the badge sat at zero while real runs waited.
   */
  const pendingApprovals = useMemo(
    () => agentCalls.filter((call) => call.status === "proposed"),
    [agentCalls],
  );

  /**
   * The generated *apps* — code the sandbox builds and publishes. Named apart
   * from `dashboards` above because they are two different things that the
   * product used to call by one name: an app is a program in an iframe, a
   * dashboard is a query and a shape the server draws.
   */
  const dashboardApps = useMemo(
    () => apps.filter((app) => app.app_type === "code"),
    [apps],
  );

  const pinnedIds = useMemo(
    () => new Set(dashboardPins.map((pin) => pin.dashboard.id)),
    [dashboardPins],
  );

  const refreshSecondary = useCallback(async () => {
    const [nextSources, nextAgentCalls, nextAudit] = await Promise.all([
      api.listSources(),
      api.listAgentToolCalls(),
      api.listAuditEvents(),
    ]);
    setSources(nextSources);
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

  /** Everything the home screen reads: what exists, what is bindable, what is pinned. */
  const refreshDashboards = useCallback(async () => {
    const [nextDashboards, nextTemplates, nextPins] = await Promise.all([
      api.listDashboards(),
      api.listDashboardTemplates(),
      api.listDashboardPins(),
    ]);
    setDashboards(nextDashboards);
    setDashboardTemplates(nextTemplates);
    setDashboardPins(nextPins);
  }, []);

  const refreshArtifacts = useCallback(async () => {
    const [nextDocuments, nextFolders, nextBoards] = await Promise.all([
      api.listDocuments(),
      api.listFolders(),
      api.listBoards(),
      // Rides along because a dashboard is an artifact of a turn like any
      // other: the agent's `create_dashboard` lands here, and this is what a
      // chat run calls when it finishes. Without it a chart the assistant just
      // made is missing from the list until the page is reloaded.
      refreshDashboards(),
    ]);
    setDocuments(nextDocuments);
    setFolders(nextFolders);
    setBoards(nextBoards);
  }, [refreshDashboards]);

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

  // Seed the composer's per-turn controls from the deployment defaults the
  // moment the bootstrap lands, but never clobber a choice the user already
  // made — the `current ||` guard is what lets the selectors show a concrete
  // model/effort without overriding a selection between reloads of the lists.
  useEffect(() => {
    if (!bootstrap) return;
    setSelectedModel((current) => current || bootstrap.model_provider.model);
    setSelectedEffort((current) => current || bootstrap.model_provider.default_effort);
  }, [bootstrap]);

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
  //
  // Re-uploading a corrected file appends a *version* rather than making a
  // second dataset — the content-hashed chain ADR 0003 specifies. This used to
  // call createDataset unconditionally, which answered the re-upload with the
  // 409 on the dataset name and swallowed it: the corrected file landed as a
  // source, and every dashboard bound to that dataset kept querying the bad
  // rows with nothing on screen to say so.
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
        const name = baseName(source.filename);
        // Matched on name because that is what the API holds unique per
        // workspace, and what made the old call collide.
        const existing = datasets.find((dataset) => dataset.name === name);
        try {
          const saved = existing
            ? await api.createDatasetVersion(existing.id, source.id)
            : await api.createDataset(name, source.id);
          setDatasets((items) => {
            const known = items.some((item) => item.id === saved.id);
            // Replace, never skip: a new version changes current_version,
            // source_id and the column schema on the same dataset id.
            return known
              ? items.map((item) => (item.id === saved.id ? saved : item))
              : [saved, ...items];
          });
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

  const folderHandlers = createFolderHandlers({ setError, setFolders, setDocuments });

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
    selectedModel,
    selectedEffort,
    fast,
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
    setDashboards,
    setDashboardPins,
    setDashboardResults,
    requested: requestedDashboards,
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
    agentCalls,
    auditEvents,
    graph,
    memories,
    datasets,
    apps,
    integrations,
    mcpServers,
    documents,
    folders,
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
    selectedModel,
    setSelectedModel,
    selectedEffort,
    setSelectedEffort,
    fast,
    setFast,
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
    dashboardApps,
    dashboards,
    dashboardTemplates,
    dashboardPins,
    dashboardResults,
    pinnedIds,
    focusedDashboard,
    setFocusedDashboard,
    loadWorkspace,
    refreshOffScreenWork,
    ...documentHandlers,
    ...folderHandlers,
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
