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
  SandboxTool,
  Skill,
  Source,
  WorkspaceDocument,
  WorkspaceProject,
} from "@workspace/api-client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import {
  CHAT_PANES_KEY,
  addPane,
  newPaneId,
  parseStoredPanes,
  prunePanes,
  refocusAfterClose,
  removePane,
  serializeStoredPanes,
  type ChatPane,
} from "./chat-panes";

export type { ChatPane } from "./chat-panes";
import { createBoardHandlers } from "./handlers/boards";
import { createChatHandlers } from "./handlers/chat";
import { createDashboardHandlers } from "./handlers/dashboards";
import { createDocumentHandlers } from "./handlers/documents";
import { createFolderHandlers } from "./handlers/folders";
import { createGraphHandlers } from "./handlers/graph";
import { createInfraHandlers } from "./handlers/infra";
import { createIntegrationHandlers } from "./handlers/integrations";
import { createMcpHandlers } from "./handlers/mcp";
import { createSandboxToolHandlers } from "./handlers/sandbox-tools";
import { createSourceHandlers } from "./handlers/sources";
import { createTodoHandlers } from "./handlers/todos";
import type { BudgetPark } from "./views/budget-format";
import type { DashboardResultState } from "./views/dashboard-grid";
import { baseName, describeError, isTabular, type View } from "./views/shared";
import { isTodoList, todoListsFrom } from "./views/todo-format";

/**
 * The persisted pane layout, or none. Guarded for private-mode / server render
 * exactly like the workspace selection: a throwing or absent store is simply an
 * empty split, never a crash. The decode itself lives in `chat-panes` so it can
 * be tested without a DOM; this wrapper only adds the `window` guard.
 */
function readStoredPanes(): ChatPane[] {
  if (typeof window === "undefined") return [];
  try {
    return parseStoredPanes(window.localStorage.getItem(CHAT_PANES_KEY));
  } catch {
    return [];
  }
}

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
  const [sandboxTools, setSandboxTools] = useState<SandboxTool[]>([]);
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
  // Extra chat panes open beside the primary shell chat, and which one holds the
  // split's focus. An empty list is today's single-pane shell — a guaranteed
  // no-regression path — so pane 0 (the shell's `activeConversation`) is
  // implicit and only the EXTRA panes live here. Persisted like the workspace
  // selection so a split survives a reload; hydrated lazily so the server render
  // never touches localStorage. `focusedPane` is null when the primary is focused.
  const [extraPanes, setExtraPanes] = useState<ChatPane[]>(() => readStoredPanes());
  const [focusedPane, setFocusedPane] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  // Which authored agent answers the next message; "" is the workspace
  // default. Session state on purpose — a conversation does not remember it.
  const [selectedAgentId, setSelectedAgentId] = useState("");
  // Per-turn model / reasoning-effort / fast overrides for the composer, session
  // state like the agent selection above. The model stays "" (the deployment's
  // own) until the user picks one; the effort seeds from the deployment default
  // once bootstrap arrives; "fast" is the low-effort shortcut.
  const [selectedModel, setSelectedModel] = useState("");
  const [selectedEffort, setSelectedEffort] = useState("");
  const [fast, setFast] = useState(false);
  // The skill attached to the next turn and the values for its declared args.
  // Per-turn session state like the controls above: the composer's slash picker
  // sets it, the send consumes it, and it never becomes part of the conversation.
  const [attachedSkill, setAttachedSkill] = useState<Skill | null>(null);
  const [skillArgs, setSkillArgs] = useState<Record<string, unknown>>({});
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
  /**
   * Runs the prompt-injection screen flagged this session, so the transcript can
   * mark the turn an injection was caught in. Kept per-run rather than per-message
   * because a flagged turn escalates to ask_all and may park before it produces a
   * message at all; the id on every message ties the mark back to the right turn.
   */
  const [flaggedRuns, setFlaggedRuns] = useState<string[]>([]);
  const recordScreenFlag = useCallback((runId: string) => {
    setFlaggedRuns((runs) => (runs.includes(runId) ? runs : [...runs, runId]));
  }, []);
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

  /**
   * The boards that are todo lists — one column, drawn as checkboxes.
   *
   * Derived rather than fetched, because that is what a list *is* on the server
   * too. It also means a list has no state of its own to fall behind: every
   * place that already refreshes boards (a finished chat turn, a workflow run,
   * the initial load) refreshes lists, and a list the agent just created shows
   * up inline in the conversation that asked for it with nothing else wired.
   */
  const todoLists = useMemo(() => todoListsFrom(boards), [boards]);

  /**
   * The rest — the boards with something to drag between.
   *
   * The two views partition `boards` rather than overlapping, so a thing is in
   * exactly one place and the badge on each tab counts what that tab shows.
   * It also makes the graduation visible: add a second column to a list and it
   * leaves Lists for Boards, same id, same cards, ticks intact.
   */
  const kanbanBoards = useMemo(
    () => boards.filter((board) => !isTodoList(board)),
    [boards],
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
      nextSandboxTools,
    ] = await Promise.all([
      api.getGraph(),
      api.listMemory(),
      api.listDatasets(),
      api.listApps(),
      api.listIntegrations(),
      api.listMcpServers(),
      api.listSandboxTools(),
    ]);
    setGraph(nextGraph);
    setMemories(nextMemories);
    setDatasets(nextDatasets);
    setApps(nextApps);
    setIntegrations(nextIntegrations);
    setMcpServers(nextMcp);
    setSandboxTools(nextSandboxTools);
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

  /** Re-read the rail's conversations list — what an extra pane's finished run
   *  needs the shell to catch up on, and nothing else. */
  const refreshConversations = useCallback(async () => {
    setConversations(await api.listConversations());
  }, []);

  /** Replace one conversation row in the rail's list. An extra pane changing its
   *  approval mode hands the updated row here so the mode keeps one home. */
  const patchConversation = useCallback((updated: Conversation) => {
    setConversations((items) =>
      items.map((item) => (item.id === updated.id ? updated : item)),
    );
  }, []);

  /**
   * Open a conversation in an extra pane beside the primary chat.
   *
   * Ignored when the cap is hit or the conversation is already the primary pane
   * — that thread is on screen, and a second copy of what pane 0 already shows
   * is not what "split" is for. Switches to Chat so the new pane is visible even
   * when the action is fired from the rail on another view.
   */
  const openInNewPane = useCallback((conversationId: string) => {
    // The early return gates the SIDE EFFECTS — a no-op open must not yank the
    // view to Chat or shut the rail. `addPane` re-checks the same guard so it
    // stays a total pure function, but the list decision and the view switch
    // are two separate concerns.
    if (conversationId === activeConversationRef.current) return;
    setExtraPanes((panes) =>
      addPane(panes, conversationId, activeConversationRef.current, newPaneId()),
    );
    setView("chat");
    setSidebarOpen(false);
  }, []);

  const closePane = useCallback((paneId: string) => {
    setExtraPanes((panes) => removePane(panes, paneId));
    setFocusedPane((current) => refocusAfterClose(current, paneId));
  }, []);

  const focusPane = useCallback((paneId: string | null) => {
    setFocusedPane(paneId);
  }, []);

  // Persist the split, and prune panes whose conversation no longer exists.
  // The persist mirrors the workspace-selection guard; the prune fires whenever
  // the conversations list changes — a delete included — but only once the list
  // has actually loaded, so a persisted split is not wiped by the empty list the
  // first render holds before `loadWorkspace` resolves.
  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      window.localStorage.setItem(CHAT_PANES_KEY, serializeStoredPanes(extraPanes));
    } catch {
      // Private mode: the split just does not survive a reload.
    }
  }, [extraPanes]);

  useEffect(() => {
    if (conversations.length === 0) return;
    const known = new Set(conversations.map((item) => item.id));
    setExtraPanes((panes) => prunePanes(panes, known));
    // A pruned pane may have held the split's focus; if the focused id no longer
    // names a pane whose conversation survived, hand focus back to the primary
    // rather than leave it pointing at a pane about to leave the screen.
    setFocusedPane((current) =>
      current &&
      !extraPanes.some((pane) => pane.id === current && known.has(pane.conversationId))
        ? null
        : current,
    );
  }, [conversations, extraPanes]);

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
        nextSandboxTools,
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
        api.listSandboxTools(),
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
      setSandboxTools(nextSandboxTools);
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

  // Seed the composer's effort from the deployment default the first time
  // bootstrap arrives, and never again — `current || preset` leaves a value the
  // user has since picked alone.
  useEffect(() => {
    const preset = bootstrap?.model_provider.default_effort;
    if (preset) setSelectedEffort((current) => current || preset);
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

  /**
   * Re-read whichever document is open *at the moment this runs*.
   *
   * The caller is the document panel's `onRunSettled`, which fires after a whole
   * turn has streamed. By then the user may well have clicked a different file,
   * and a handler built around the `activeDocument` of the render that started
   * the turn would reopen the one they left — silently replacing what they are
   * looking at with what they were looking at. The rail's twin reads
   * `activeDocumentRef` for exactly this reason; so does this.
   */
  const reloadOpenDocument = async () => {
    const open = activeDocumentRef.current;
    if (open) await documentHandlers.openDocument(open);
    await refreshArtifacts();
  };

  const folderHandlers = createFolderHandlers({ setError, setFolders, setDocuments });

  const boardHandlers = createBoardHandlers({ setError, setBoards });

  const todoHandlers = createTodoHandlers({ setError, setBoards });

  const infraHandlers = createInfraHandlers({
    setError,
    setDbConnections,
    setProjects,
    setActiveProject,
  });

  const mcpHandlers = createMcpHandlers({ setError, setMcpServers });

  const sandboxToolHandlers = createSandboxToolHandlers({ setError, setSandboxTools });

  /**
   * The composer's slash-picker actions. Attaching seeds each declared arg with
   * its default so a skill with sensible defaults is sendable on sight; clearing
   * drops both the skill and its args, which is what the send does per-turn.
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

  const chatHandlers = createChatHandlers({
    bootstrap,
    conversations,
    messages,
    draft,
    activeConversation,
    activeRun,
    selectedAgentId,
    setSelectedAgentId,
    selectedModel,
    selectedEffort,
    fast,
    attachedSkill,
    skillArgs,
    clearAttachedSkill: detachSkill,
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
    onScreenFlag: recordScreenFlag,
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
    sandboxTools,
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
    // Multi-pane split: the extra panes beside the primary chat, which one is
    // focused, and the actions over them. An empty `extraPanes` is the shell as
    // it has always been.
    extraPanes,
    focusedPane,
    openInNewPane,
    closePane,
    focusPane,
    refreshConversations,
    patchConversation,
    draft,
    setDraft,
    selectedAgentId,
    setSelectedAgentId,
    selectedModel,
    setSelectedModel,
    selectedEffort,
    setSelectedEffort,
    fast,
    setFast,
    attachedSkill,
    skillArgs,
    attachSkill,
    detachSkill,
    setSkillArg,
    activeRun,
    runStatus,
    budgetPark,
    flaggedRuns,
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
    todoLists,
    kanbanBoards,
    focusedDashboard,
    setFocusedDashboard,
    loadWorkspace,
    refreshOffScreenWork,
    // Both exposed for the chat panel beside a document, which has to refetch
    // the pending edits its inline review renders and the list the document's
    // title appears in — and nothing else, which is why it does not get
    // `refreshOffScreenWork`.
    refreshArtifacts,
    refreshPendingEdits,
    reloadOpenDocument,
    ...documentHandlers,
    ...folderHandlers,
    ...boardHandlers,
    ...todoHandlers,
    ...infraHandlers,
    ...mcpHandlers,
    ...sandboxToolHandlers,
    ...chatHandlers,
    ...sourceHandlers,
    ...graphHandlers,
    ...dashboardHandlers,
    ...integrationHandlers,
  };
}
