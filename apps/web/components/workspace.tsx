"use client";

import {
  Activity,
  AppWindow,
  ArrowUp,
  BarChart3,
  Check,
  CircleDot,
  Database,
  File,
  FileText,
  Globe2,
  Library,
  LockKeyhole,
  Menu,
  MessageSquare,
  MoreHorizontal,
  Network,
  Paperclip,
  Plug,
  Plus,
  RefreshCw,
  ShieldCheck,
  Trash2,
  UploadCloud,
  X,
} from "lucide-react";
import type {
  AppPreview,
  AuditEvent,
  Bootstrap,
  Citation,
  Conversation,
  Dashboard,
  DashboardRun,
  DashboardSpec,
  Dataset,
  GeneratedApp,
  IntegrationProvider,
  KnowledgeGraph,
  MemoryItem,
  Message,
  ProvenanceChunk,
  Source,
  ToolCall,
} from "@workspace/api-client";
import { WorkspaceApi } from "@workspace/api-client";
import { FormEvent, MouseEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { ApiHealthBanner } from "./api-health-banner";
import { SandboxFrame } from "./sandbox-frame";
import { Snapshot } from "./snapshot-renderer";

type View =
  | "chat"
  | "sources"
  | "graph"
  | "analytics"
  | "apps"
  | "integrations"
  | "activity";

const api = new WorkspaceApi(
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000",
);

const starterPrompts = [
  "Summarize the indexed sources.",
  "List the main facts and their citations.",
  "/tool github-zen",
];

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function formatRelative(value: string): string {
  const delta = Date.now() - new Date(value).getTime();
  const minutes = Math.max(0, Math.floor(delta / 60_000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return new Date(value).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
}

function statusLabel(status: Source["status"]): string {
  if (status === "ready") return "Indexed";
  if (status === "processing") return "Reading";
  if (status === "queued") return "Queued";
  if (status === "failed") return "Needs attention";
  return status;
}

export function Workspace() {
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeConversation, setActiveConversation] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [sources, setSources] = useState<Source[]>([]);
  const [toolCalls, setToolCalls] = useState<ToolCall[]>([]);
  const [auditEvents, setAuditEvents] = useState<AuditEvent[]>([]);
  const [graph, setGraph] = useState<KnowledgeGraph | null>(null);
  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [dashboards, setDashboards] = useState<Dashboard[]>([]);
  const [dashboardRuns, setDashboardRuns] = useState<Record<string, DashboardRun>>({});
  const [apps, setApps] = useState<GeneratedApp[]>([]);
  const [integrations, setIntegrations] = useState<IntegrationProvider[]>([]);
  const [view, setView] = useState<View>("chat");
  const [draft, setDraft] = useState("");
  const [activeRun, setActiveRun] = useState<string | null>(null);
  const [runStatus, setRunStatus] = useState("");
  const [provenance, setProvenance] = useState<ProvenanceChunk | null>(null);
  const [loadingProvenance, setLoadingProvenance] = useState(false);
  const [appPreview, setAppPreview] = useState<AppPreview | null>(null);
  const [studioAppId, setStudioAppId] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [error, setError] = useState("");
  const endRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const activeConversationRef = useRef<string | null>(null);

  const pendingApprovals = useMemo(
    () => toolCalls.filter((call) => call.status === "proposed"),
    [toolCalls],
  );

  const refreshSecondary = useCallback(async () => {
    const [nextSources, nextTools, nextAudit] = await Promise.all([
      api.listSources(),
      api.listToolCalls(),
      api.listAuditEvents(),
    ]);
    setSources(nextSources);
    setToolCalls(nextTools);
    setAuditEvents(nextAudit);
  }, []);

  const refreshExpansion = useCallback(async () => {
    const [
      nextGraph,
      nextMemories,
      nextDatasets,
      nextDashboards,
      nextApps,
      nextIntegrations,
    ] = await Promise.all([
      api.getGraph(),
      api.listMemory(),
      api.listDatasets(),
      api.listDashboards(),
      api.listApps(),
      api.listIntegrations(),
    ]);
    setGraph(nextGraph);
    setMemories(nextMemories);
    setDatasets(nextDatasets);
    setDashboards(nextDashboards);
    setApps(nextApps);
    setIntegrations(nextIntegrations);
  }, []);

  const loadWorkspace = useCallback(async () => {
    try {
      const [
        boot,
        chats,
        nextSources,
        nextTools,
        nextAudit,
        nextGraph,
        nextMemories,
        nextDatasets,
        nextDashboards,
        nextApps,
        nextIntegrations,
      ] = await Promise.all([
        api.bootstrap(),
        api.listConversations(),
        api.listSources(),
        api.listToolCalls(),
        api.listAuditEvents(),
        api.getGraph(),
        api.listMemory(),
        api.listDatasets(),
        api.listDashboards(),
        api.listApps(),
        api.listIntegrations(),
      ]);
      setBootstrap(boot);
      setConversations(chats);
      setSources(nextSources);
      setToolCalls(nextTools);
      setAuditEvents(nextAudit);
      setGraph(nextGraph);
      setMemories(nextMemories);
      setDatasets(nextDatasets);
      setDashboards(nextDashboards);
      setApps(nextApps);
      setIntegrations(nextIntegrations);
      setError("");
      if (chats[0] && !activeConversationRef.current) {
        setActiveConversation(chats[0].id);
        activeConversationRef.current = chats[0].id;
        setMessages(await api.listMessages(chats[0].id));
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not load workspace");
    }
  }, []);

  useEffect(() => {
    void loadWorkspace();
  }, [loadWorkspace]);

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

  async function selectConversation(id: string) {
    setActiveConversation(id);
    activeConversationRef.current = id;
    setSidebarOpen(false);
    setView("chat");
    setError("");
    try {
      setMessages(await api.listMessages(id));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not open conversation");
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
      setError(caught instanceof Error ? caught.message : "Could not create conversation");
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
      setError(caught instanceof Error ? caught.message : "Could not delete chat");
    }
  }

  async function followRun(runId: string, conversationId: string) {
    const temporaryId = `streaming-${runId}`;
    setActiveRun(runId);
    setRunStatus("Starting");
    try {
      for await (const event of api.streamRun(runId)) {
        if (event.event === "run.started") setRunStatus("Searching sources");
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
                created_at: new Date().toISOString(),
              },
            ];
          });
        }
        if (event.event === "tool.proposed") {
          setRunStatus("Waiting for your approval");
          await refreshSecondary();
        }
        if (event.event === "tool.started") setRunStatus("Running approved read-only tool");
        if (event.event === "message.completed") {
          const completed: Message = {
            id: String(event.data.message_id),
            run_id: runId,
            role: "assistant",
            content: String(event.data.content || ""),
            citations: (event.data.citations || []) as Citation[],
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
          setError(String(event.data.error || "The run failed"));
        }
      }
      if (activeConversationRef.current === conversationId) {
        setMessages(await api.listMessages(conversationId));
      }
      setConversations(await api.listConversations());
      await refreshSecondary();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The event stream disconnected");
    } finally {
      setActiveRun((current) => (current === runId ? null : current));
      setRunStatus("");
    }
  }

  async function submitPrompt(event?: FormEvent, promptOverride?: string) {
    event?.preventDefault();
    const content = (promptOverride ?? draft).trim();
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
      setError(caught instanceof Error ? caught.message : "Could not send message");
    }
  }

  async function openChunk(chunkId: string) {
    setLoadingProvenance(true);
    setError("");
    try {
      setProvenance(await api.getChunk(chunkId));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not load provenance");
    } finally {
      setLoadingProvenance(false);
    }
  }

  async function openCitation(citation: Citation) {
    await openChunk(citation.chunk_id);
  }

  async function uploadFiles(files: FileList | File[]) {
    const file = Array.from(files)[0];
    if (!file) return;
    setUploading(true);
    setError("");
    // Switch immediately so later refreshes never clobber user navigation.
    setView("sources");
    try {
      await api.uploadSource(file);
      await refreshSecondary();
      await refreshExpansion();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Upload failed");
    } finally {
      setUploading(false);
      setDragging(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  async function removeSource(source: Source) {
    if (!window.confirm(`Delete “${source.filename}” and all indexed passages?`)) return;
    setError("");
    try {
      await api.deleteSource(source.id);
      await refreshSecondary();
      await refreshExpansion();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not delete source");
    }
  }

  async function decide(call: ToolCall, decision: "approved" | "denied") {
    setError("");
    try {
      await api.decideToolCall(call.id, decision);
      await refreshSecondary();
      if (!activeRun && call.conversation_id === activeConversation) {
        // Resume streaming the run we just unblocked; runs in other
        // conversations continue server-side and load on next open.
        void followRun(call.run_id, call.conversation_id);
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not record decision");
    }
  }

  async function rebuildKnowledgeGraph() {
    setError("");
    try {
      setGraph(await api.rebuildGraph());
      for (let attempt = 0; attempt < 30; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 400));
        const next = await api.getGraph();
        setGraph(next);
        if (!["queued", "building"].includes(next.status)) break;
      }
      await refreshSecondary();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not rebuild graph");
    }
  }

  async function createDatasetFromSource(
    sourceId: string,
    name: string,
    description: string,
  ) {
    setError("");
    try {
      await api.createDataset(name, sourceId, description);
      await refreshExpansion();
      await refreshSecondary();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not create dataset");
      throw caught;
    }
  }

  async function createDashboard(payload: {
    name: string;
    description?: string;
    dataset_id: string;
    spec: DashboardSpec;
  }) {
    setError("");
    try {
      const created = await api.createDashboard(payload);
      setDashboards((items) => [created, ...items]);
      const run = await api.runDashboard(created.id);
      setDashboardRuns((items) => ({ ...items, [created.id]: run }));
      await refreshSecondary();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not create dashboard");
      throw caught;
    }
  }

  async function createDatasetVersion(datasetId: string, sourceId: string) {
    setError("");
    try {
      await api.createDatasetVersion(datasetId, sourceId);
      await refreshExpansion();
      await refreshSecondary();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not create dataset version");
    }
  }

  async function runDashboard(dashboardId: string) {
    setError("");
    try {
      const run = await api.runDashboard(dashboardId);
      setDashboardRuns((items) => ({ ...items, [dashboardId]: run }));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not run dashboard");
    }
  }

  async function createGeneratedApp(payload: {
    name: string;
    slug: string;
    description?: string;
    visibility: "private" | "public";
    app_type?: "dashboard" | "code";
    dashboard_ids: string[];
  }) {
    setError("");
    try {
      const created = await api.createApp(payload);
      setApps((items) => [created, ...items]);
      await refreshSecondary();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not create app");
      throw caught;
    }
  }

  async function createGeneratedAppRelease(app: GeneratedApp, dashboardIds: string[]) {
    setError("");
    try {
      const updated = await api.createAppRelease(app.id, dashboardIds);
      setApps((items) => items.map((item) => (item.id === updated.id ? updated : item)));
      await refreshSecondary();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not create release");
    }
  }

  async function publishGeneratedApp(app: GeneratedApp, releaseId: string) {
    setError("");
    try {
      const updated = await api.publishAppRelease(app.id, releaseId);
      setApps((items) => items.map((item) => (item.id === updated.id ? updated : item)));
      await refreshSecondary();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not publish release");
    }
  }

  async function rollbackGeneratedApp(app: GeneratedApp, releaseId: string) {
    setError("");
    try {
      const updated = await api.rollbackAppRelease(app.id, releaseId);
      setApps((items) => items.map((item) => (item.id === updated.id ? updated : item)));
      await refreshSecondary();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not roll back release");
    }
  }

  async function previewGeneratedApp(app: GeneratedApp) {
    setError("");
    try {
      setAppPreview(await api.previewApp(app.id));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not load app preview");
    }
  }

  async function connectIntegration(provider: string) {
    setError("");
    try {
      const { authorize_url } = await api.connectIntegration(provider);
      window.location.href = authorize_url;
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not start OAuth");
    }
  }

  async function connectGarminAccount(email: string, password: string) {
    setError("");
    await api.connectGarmin(email, password);
    setIntegrations(await api.listIntegrations());
  }

  async function disconnectIntegration(accountId: string) {
    if (!window.confirm("Disconnect this account and remove its stored tokens?")) return;
    setError("");
    try {
      await api.disconnectIntegration(accountId);
      setIntegrations(await api.listIntegrations());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not disconnect");
    }
  }

  async function syncIntegration(accountId: string) {
    setError("");
    try {
      await api.syncIntegration(accountId);
      for (let attempt = 0; attempt < 30; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
        const jobs = await api.listSyncJobs(accountId);
        if (jobs[0] && !["queued", "running"].includes(jobs[0].status)) {
          if (jobs[0].status === "failed") {
            setError(jobs[0].error || "Sync failed");
          }
          break;
        }
      }
      await refreshSecondary();
      await refreshExpansion();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not sync");
    }
  }

  async function generateAppCode(app: GeneratedApp, prompt: string, datasetIds: string[]) {
    setError("");
    const updated = await api.generateApp(app.id, {
      prompt,
      dataset_ids: datasetIds,
    });
    setApps((items) => items.map((item) => (item.id === updated.id ? updated : item)));
    await refreshSecondary();
  }

  async function forgetMemory(item: MemoryItem) {
    if (!window.confirm("Forget this memory? It will not be recalled again.")) return;
    setError("");
    try {
      await api.deleteMemory(item.id);
      setMemories((items) => items.filter((memory) => memory.id !== item.id));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not forget memory");
    }
  }

  const activeTitle =
    conversations.find((item) => item.id === activeConversation)?.title || "New conversation";

  return (
    <div className="workspace-shell">
      <aside className={`sidebar ${sidebarOpen ? "sidebar-open" : ""}`}>
        <div className="sidebar-top">
          <button
            className="icon-button mobile-close"
            onClick={() => setSidebarOpen(false)}
            aria-label="Close menu"
          >
            <X size={18} />
          </button>
        </div>

        <button className="new-thread-button" onClick={newConversation}>
          <Plus size={16} />
          New thread
          <span>⌘ K</span>
        </button>

        <nav className="primary-nav" aria-label="Workspace">
          <button
            className={view === "chat" ? "nav-item active" : "nav-item"}
            onClick={() => {
              setView("chat");
              setSidebarOpen(false);
            }}
          >
            <MessageSquare size={17} />
            Chat
          </button>
          <button
            className={view === "sources" ? "nav-item active" : "nav-item"}
            onClick={() => {
              setView("sources");
              setSidebarOpen(false);
            }}
          >
            <Library size={17} />
            Sources
            <span className="nav-count">{sources.length}</span>
          </button>
          <button
            className={view === "graph" ? "nav-item active" : "nav-item"}
            onClick={() => {
              setView("graph");
              setSidebarOpen(false);
            }}
          >
            <Network size={17} />
            Graph
            <span className="nav-count">{graph?.entities.length || 0}</span>
          </button>
          <button
            className={view === "analytics" ? "nav-item active" : "nav-item"}
            onClick={() => {
              setView("analytics");
              setSidebarOpen(false);
            }}
          >
            <BarChart3 size={17} />
            Dashboards
            <span className="nav-count">{dashboards.length}</span>
          </button>
          <button
            className={view === "apps" ? "nav-item active" : "nav-item"}
            onClick={() => {
              setView("apps");
              setSidebarOpen(false);
            }}
          >
            <AppWindow size={17} />
            Apps
            <span className="nav-count">{apps.length}</span>
          </button>
          <button
            className={view === "integrations" ? "nav-item active" : "nav-item"}
            onClick={() => {
              setView("integrations");
              setSidebarOpen(false);
            }}
          >
            <Plug size={17} />
            Integrations
            <span className="nav-count">
              {integrations.filter((item) => item.account).length}
            </span>
          </button>
          <button
            className={view === "activity" ? "nav-item active" : "nav-item"}
            onClick={() => {
              setView("activity");
              setSidebarOpen(false);
            }}
          >
            <Activity size={17} />
            Activity
            {pendingApprovals.length > 0 && (
              <span className="approval-count">{pendingApprovals.length}</span>
            )}
          </button>
        </nav>

        <div className="thread-heading">
          <span>Recent threads</span>
          <MoreHorizontal size={15} />
        </div>
        <div className="thread-list">
          {conversations.length === 0 ? (
            <p className="empty-threads">No conversations.</p>
          ) : (
            conversations.map((conversation) => (
              <div
                key={conversation.id}
                className={
                  activeConversation === conversation.id ? "thread active" : "thread"
                }
              >
                <button
                  className="thread-open"
                  onClick={() => selectConversation(conversation.id)}
                >
                  <span>{conversation.title}</span>
                  <time>{formatRelative(conversation.updated_at)}</time>
                </button>
                <button
                  className="thread-delete"
                  title="Delete chat"
                  aria-label={`Delete ${conversation.title}`}
                  onClick={(event) => void removeConversation(conversation, event)}
                >
                  <Trash2 size={13} />
                </button>
              </div>
            ))
          )}
        </div>

        <div className="workspace-identity">
          <div className="avatar">
            {bootstrap?.identity.user_name.slice(0, 1).toUpperCase() || "U"}
          </div>
          <div>
            <strong>{bootstrap?.identity.user_name || "Connecting…"}</strong>
            <span>Signed in</span>
          </div>
        </div>
      </aside>

      {sidebarOpen && (
        <button
          className="sidebar-scrim"
          aria-label="Close menu"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      <main className="main-panel">
        <ApiHealthBanner api={api} onRecovered={loadWorkspace} />
        <header className="topbar">
          <button
            className="icon-button menu-button"
            onClick={() => setSidebarOpen(true)}
            aria-label="Open menu"
          >
            <Menu size={19} />
          </button>
          <div className="page-context">
            <strong>
              {view === "chat"
                ? activeTitle
                : view === "sources"
                  ? "Sources"
                  : view === "graph"
                    ? "Graph"
                    : view === "analytics"
                      ? "Dashboards"
                      : view === "apps"
                        ? "Apps"
                        : view === "integrations"
                          ? "Integrations"
                          : "Activity"}
            </strong>
          </div>
          <div className="topbar-actions">
            <div
              className="agent-pill"
              title={
                bootstrap?.model_provider.provider === "openai"
                  ? "OpenAI provider"
                  : "Deterministic local provider"
              }
            >
              {bootstrap?.model_provider.model || "Loading provider"}
            </div>
            <div className="local-status">
              <span />
              Connected
            </div>
          </div>
        </header>

        {error && (
          <div className="error-toast" role="alert">
            <CircleDot size={15} />
            <span>{error}</span>
            <button onClick={() => setError("")} aria-label="Dismiss">
              <X size={14} />
            </button>
          </div>
        )}

        {view === "chat" && (
          <ChatView
            messages={messages}
            sources={sources}
            draft={draft}
            setDraft={setDraft}
            activeRun={activeRun}
            runStatus={runStatus}
            submitPrompt={submitPrompt}
            openCitation={openCitation}
            setView={setView}
            endRef={endRef}
          />
        )}

        {view === "sources" && (
          <SourcesView
            sources={sources}
            uploading={uploading}
            dragging={dragging}
            setDragging={setDragging}
            uploadFiles={uploadFiles}
            removeSource={removeSource}
            fileInputRef={fileInputRef}
          />
        )}

        {view === "graph" && (
          <GraphView
            graph={graph}
            memories={memories}
            rebuild={rebuildKnowledgeGraph}
            openChunk={openChunk}
            forgetMemory={forgetMemory}
          />
        )}

        {view === "analytics" && (
          <AnalyticsView
            sources={sources}
            datasets={datasets}
            dashboards={dashboards}
            runs={dashboardRuns}
            createDataset={createDatasetFromSource}
            createDatasetVersion={createDatasetVersion}
            createDashboard={createDashboard}
            runDashboard={runDashboard}
          />
        )}

        {view === "apps" && (
          <AppsView
            apps={apps}
            dashboards={dashboards}
            createApp={createGeneratedApp}
            createRelease={createGeneratedAppRelease}
            publish={publishGeneratedApp}
            rollback={rollbackGeneratedApp}
            preview={previewGeneratedApp}
            openStudio={(app) => setStudioAppId(app.id)}
          />
        )}

        {view === "integrations" && (
          <IntegrationsView
            integrations={integrations}
            connect={connectIntegration}
            connectGarmin={connectGarminAccount}
            disconnect={disconnectIntegration}
            sync={syncIntegration}
            setError={setError}
          />
        )}

        {view === "activity" && (
          <ActivityView
            calls={toolCalls}
            events={auditEvents}
            decide={decide}
            activeRun={activeRun}
          />
        )}
      </main>

      {studioAppId && (
        <AppStudio
          app={apps.find((item) => item.id === studioAppId) || null}
          datasets={datasets}
          generate={generateAppCode}
          publish={publishGeneratedApp}
          onClose={() => setStudioAppId(null)}
          setError={setError}
        />
      )}

      {appPreview && (
        <div className="drawer-scrim" onClick={() => setAppPreview(null)}>
          <aside
            className="provenance-drawer app-preview-drawer"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="drawer-header">
              <div>
                <span>Draft preview · v{appPreview.version}</span>
                <strong>{appPreview.name}</strong>
              </div>
              <button
                className="icon-button"
                onClick={() => setAppPreview(null)}
                aria-label="Close preview"
              >
                <X size={18} />
              </button>
            </div>
            <div className="app-preview-content">
              {(appPreview.manifest.dashboards || []).map((dashboard) => (
                <section className="published-card" key={dashboard.id}>
                  <header>
                    <h2>{dashboard.name}</h2>
                    {dashboard.description && <p>{dashboard.description}</p>}
                  </header>
                  <Snapshot dashboard={dashboard} />
                </section>
              ))}
            </div>
          </aside>
        </div>
      )}

      {(provenance || loadingProvenance) && (
        <div className="drawer-scrim" onClick={() => setProvenance(null)}>
          <aside className="provenance-drawer" onClick={(event) => event.stopPropagation()}>
            <div className="drawer-header">
              <div>
                <span>Source provenance</span>
                <strong>{provenance?.filename || "Loading passage…"}</strong>
              </div>
              <button
                className="icon-button"
                onClick={() => setProvenance(null)}
                aria-label="Close provenance"
              >
                <X size={18} />
              </button>
            </div>
            {provenance && (
              <>
                <div className="provenance-meta">
                  <span>Passage {provenance.ordinal + 1}</span>
                  <span>
                    Characters {provenance.char_start.toLocaleString()}–
                    {provenance.char_end.toLocaleString()}
                  </span>
                </div>
                <div className="provenance-content">{provenance.content}</div>
                <div className="provenance-foot">
                  <ShieldCheck size={15} />
                  Indexed from the stored original. No generated text.
                </div>
              </>
            )}
          </aside>
        </div>
      )}
    </div>
  );
}

type ChatViewProps = {
  messages: Message[];
  sources: Source[];
  draft: string;
  setDraft: (value: string) => void;
  activeRun: string | null;
  runStatus: string;
  submitPrompt: (event?: FormEvent, promptOverride?: string) => Promise<void>;
  openCitation: (citation: Citation) => Promise<void>;
  setView: (view: View) => void;
  endRef: React.RefObject<HTMLDivElement | null>;
};

function ChatView({
  messages,
  sources,
  draft,
  setDraft,
  activeRun,
  runStatus,
  submitPrompt,
  openCitation,
  setView,
  endRef,
}: ChatViewProps) {
  return (
    <section className="chat-layout">
      <div className={`message-scroll ${messages.length === 0 ? "empty" : ""}`}>
        {messages.length === 0 ? (
          <div className="welcome">
            <h1>New conversation</h1>
            <p>Ask a question about the indexed sources.</p>
            <div className="prompt-grid">
              {starterPrompts.map((prompt) => (
                <button key={prompt} onClick={() => void submitPrompt(undefined, prompt)}>
                  <span>{prompt}</span>
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="message-column">
            {messages.map((message) => (
              <article key={message.id} className={`message ${message.role}`}>
                <div className="message-author">
                  {message.role === "user" ? (
                    <div className="tiny-avatar">U</div>
                  ) : (
                    <div className="assistant-mark">A</div>
                  )}
                  <span>{message.role === "user" ? "You" : "Assistant"}</span>
                </div>
                <div className="message-body">
                  {message.role === "assistant" ? (
                    <ReactMarkdown>{message.content}</ReactMarkdown>
                  ) : (
                    <p>{message.content}</p>
                  )}
                </div>
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
              </article>
            ))}
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
            <div ref={endRef} />
          </div>
        )}
      </div>

      <div className="composer-zone">
        <form className="composer" onSubmit={(event) => void submitPrompt(event)}>
          <textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                void submitPrompt();
              }
            }}
            placeholder={
              sources.some((source) => source.status === "ready")
                ? "Ask your workspace…"
                : "Upload a source, then ask a question…"
            }
            rows={1}
            disabled={Boolean(activeRun)}
          />
          <div className="composer-tools">
            <button type="button" onClick={() => setView("sources")} title="Add a source">
              <Paperclip size={17} />
            </button>
            <span className="composer-hint">
              <kbd>/tool</kbd>
              requires approval
            </span>
            <button
              className="send-button"
              type="submit"
              disabled={!draft.trim() || Boolean(activeRun)}
              aria-label="Send message"
            >
              <ArrowUp size={18} />
            </button>
          </div>
        </form>
        <p className="composer-caption">
          Responses use indexed passages. Verify citations when accuracy matters.
        </p>
      </div>
    </section>
  );
}

type SourcesViewProps = {
  sources: Source[];
  uploading: boolean;
  dragging: boolean;
  setDragging: (value: boolean) => void;
  uploadFiles: (files: FileList | File[]) => Promise<void>;
  removeSource: (source: Source) => Promise<void>;
  fileInputRef: React.RefObject<HTMLInputElement | null>;
};

function SourcesView({
  sources,
  uploading,
  dragging,
  setDragging,
  uploadFiles,
  removeSource,
  fileInputRef,
}: SourcesViewProps) {
  return (
    <section className="content-page">
      <div className="page-heading">
        <div>
          <h1>Sources</h1>
          <p>Files available for retrieval and citation.</p>
        </div>
        <button className="primary-button" onClick={() => fileInputRef.current?.click()}>
          <Plus size={16} />
          Add source
        </button>
      </div>

      <div
        className={`drop-zone ${dragging ? "dragging" : ""}`}
        onDragEnter={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragOver={(event) => event.preventDefault()}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          void uploadFiles(event.dataTransfer.files);
        }}
        onClick={() => fileInputRef.current?.click()}
      >
        <input
          ref={fileInputRef}
          type="file"
          accept=".txt,.md,.markdown,.pdf,.csv,.json"
          hidden
          onChange={(event) => event.target.files && void uploadFiles(event.target.files)}
        />
        <div className="upload-icon">
          <UploadCloud size={21} />
        </div>
        <div>
          <strong>{uploading ? "Indexing your source…" : "Drop a source here"}</strong>
          <span>Markdown, text, PDF, CSV, or JSON · up to 10 MB</span>
        </div>
        <button type="button">{uploading ? "Working…" : "Browse"}</button>
      </div>

      <div className="library-summary">
        <div>
          <strong>{sources.length}</strong>
          <span>sources</span>
        </div>
        <div>
          <strong>{sources.reduce((sum, source) => sum + source.chunk_count, 0)}</strong>
          <span>indexed passages</span>
        </div>
        <div>
          <strong>{formatBytes(sources.reduce((sum, source) => sum + source.byte_size, 0))}</strong>
          <span>stored originals</span>
        </div>
      </div>

      <div className="source-table">
        <div className="source-table-head">
          <span>Source</span>
          <span>Status</span>
          <span>Passages</span>
          <span>Added</span>
          <span />
        </div>
        {sources.length === 0 ? (
          <div className="table-empty">
            <Library size={24} />
            <strong>No sources yet</strong>
            <span>Add a document to make it searchable.</span>
          </div>
        ) : (
          sources.map((source) => (
            <div className="source-row" key={source.id}>
              <div className="source-name">
                <div className="file-icon">
                  <File size={17} />
                </div>
                <span>
                  <strong>{source.filename}</strong>
                  <small>{formatBytes(source.byte_size)}</small>
                </span>
              </div>
              <div>
                <span className={`source-status ${source.status}`}>
                  <i />
                  {statusLabel(source.status)}
                </span>
                {source.error && <small className="source-error">{source.error}</small>}
              </div>
              <span className="muted-cell">{source.chunk_count || "—"}</span>
              <span className="muted-cell">{formatRelative(source.created_at)}</span>
              <button
                className="delete-button"
                onClick={() => void removeSource(source)}
                title="Delete source"
              >
                <Trash2 size={15} />
              </button>
            </div>
          ))
        )}
      </div>
    </section>
  );
}

type GraphViewProps = {
  graph: KnowledgeGraph | null;
  memories: MemoryItem[];
  rebuild: () => Promise<void>;
  openChunk: (chunkId: string) => Promise<void>;
  forgetMemory: (item: MemoryItem) => Promise<void>;
};

function GraphView({ graph, memories, rebuild, openChunk, forgetMemory }: GraphViewProps) {
  const nodes = (graph?.entities || []).slice(0, 24);
  const positions = new Map(
    nodes.map((node, index) => {
      const angle = (index / Math.max(nodes.length, 1)) * Math.PI * 2 - Math.PI / 2;
      const radius = nodes.length < 8 ? 150 : 205;
      return [
        node.id,
        {
          x: 450 + Math.cos(angle) * radius,
          y: 260 + Math.sin(angle) * radius,
        },
      ] as const;
    }),
  );
  const edges = (graph?.edges || []).filter(
    (edge) => positions.has(edge.from_entity_id) && positions.has(edge.to_entity_id),
  );
  const names = new Map(nodes.map((node) => [node.id, node.name]));

  return (
    <section className="content-page graph-page">
      <div className="page-heading">
        <div>
          <h1>Knowledge graph</h1>
          <p>Rebuildable entities and co-occurrence links derived from indexed passages.</p>
        </div>
        <button
          className="secondary-button"
          onClick={() => void rebuild()}
          disabled={graph?.status === "queued" || graph?.status === "building"}
        >
          <RefreshCw
            size={15}
            className={graph?.status === "queued" || graph?.status === "building" ? "spin" : ""}
          />
          Rebuild
        </button>
      </div>

      <div className="graph-summary">
        <div>
          <strong>{graph?.entities.length || 0}</strong>
          <span>entities shown</span>
        </div>
        <div>
          <strong>{graph?.edges.length || 0}</strong>
          <span>links shown</span>
        </div>
        <div>
          <strong>{memories.length}</strong>
          <span>long-term memories</span>
        </div>
        <div>
          <strong>{graph?.status || "loading"}</strong>
          <span>{graph?.built_at ? `built ${formatRelative(graph.built_at)}` : "projection state"}</span>
        </div>
      </div>

      {nodes.length === 0 ? (
        <div className="feature-empty">
          <Network size={25} />
          <strong>No graph projection yet</strong>
          <span>Index a source or rebuild the projection.</span>
        </div>
      ) : (
        <div className="graph-layout">
          <div className="graph-canvas">
            <svg viewBox="0 0 900 520" role="img" aria-label="Workspace entity graph">
              {edges.map((edge) => {
                const from = positions.get(edge.from_entity_id);
                const to = positions.get(edge.to_entity_id);
                if (!from || !to) return null;
                return (
                  <g key={edge.id}>
                    <line
                      x1={from.x}
                      y1={from.y}
                      x2={to.x}
                      y2={to.y}
                      strokeWidth={Math.min(5, 1 + edge.weight)}
                    />
                    <title>
                      {names.get(edge.from_entity_id)} → {names.get(edge.to_entity_id)} ·{" "}
                      {edge.weight} shared passage{edge.weight === 1 ? "" : "s"}
                    </title>
                  </g>
                );
              })}
              {nodes.map((node) => {
                const point = positions.get(node.id);
                if (!point) return null;
                const label =
                  node.name.length > 22 ? `${node.name.slice(0, 20)}…` : node.name;
                return (
                  <g key={node.id} transform={`translate(${point.x} ${point.y})`}>
                    <circle r={Math.min(28, 15 + node.mention_count * 2)} />
                    <text textAnchor="middle" y={42}>
                      {label}
                    </text>
                    <title>
                      {node.name} · {node.mention_count} mentions
                    </title>
                  </g>
                );
              })}
            </svg>
          </div>
          <div className="entity-list">
            <div className="panel-title">
              <strong>Entities</strong>
              <span className="panel-count">{graph?.entities.length || 0}</span>
            </div>
            {nodes.map((entity) => (
              <div className="entity-row" key={entity.id}>
                <div>
                  <strong>{entity.name}</strong>
                  <span>
                    {entity.entity_type.replaceAll("_", " ")} · {entity.mention_count} mentions
                    {entity.memory_ids.length > 0 && " · from memory"}
                  </span>
                </div>
                {entity.chunk_ids[0] && (
                  <button onClick={() => void openChunk(entity.chunk_ids[0])}>Passage</button>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="memory-panel">
        <div className="panel-title">
          <div>
            <strong>Long-term memory</strong>
            <p>Durable notes learned from your conversations. Recalled automatically in chat.</p>
          </div>
          <span className="panel-count">{memories.length}</span>
        </div>
        {memories.length === 0 ? (
          <div className="feature-empty">
            <Network size={20} />
            <strong>No memories yet</strong>
            <span>Chat with the assistant and durable facts will accumulate here.</span>
          </div>
        ) : (
          <div className="memory-list">
            {memories.map((item) => (
              <div className="memory-row" key={item.id}>
                <div>
                  <span className={`memory-kind ${item.kind}`}>
                    {item.kind.replaceAll("_", " ")}
                  </span>
                  <p>{item.content}</p>
                  <small>
                    {item.importance > 1 && `reinforced ×${item.importance} · `}
                    updated {formatRelative(item.updated_at)}
                  </small>
                </div>
                <button
                  className="delete-button"
                  title="Forget this memory"
                  onClick={() => void forgetMemory(item)}
                >
                  <Trash2 size={14} />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

type AnalyticsViewProps = {
  sources: Source[];
  datasets: Dataset[];
  dashboards: Dashboard[];
  runs: Record<string, DashboardRun>;
  createDataset: (sourceId: string, name: string, description: string) => Promise<void>;
  createDatasetVersion: (datasetId: string, sourceId: string) => Promise<void>;
  createDashboard: (payload: {
    name: string;
    description?: string;
    dataset_id: string;
    spec: DashboardSpec;
  }) => Promise<void>;
  runDashboard: (dashboardId: string) => Promise<void>;
};

function AnalyticsView({
  sources,
  datasets,
  dashboards,
  runs,
  createDataset,
  createDatasetVersion,
  createDashboard,
  runDashboard,
}: AnalyticsViewProps) {
  const tabularSources = sources.filter(
    (source) =>
      source.status === "ready" && /\.(csv|json)$/i.test(source.filename),
  );
  const [sourceId, setSourceId] = useState("");
  const [datasetName, setDatasetName] = useState("");
  const [datasetDescription, setDatasetDescription] = useState("");
  const [datasetId, setDatasetId] = useState("");
  const [dashboardName, setDashboardName] = useState("");
  const [visualization, setVisualization] =
    useState<DashboardSpec["visualization"]>("bar");
  const [groupBy, setGroupBy] = useState("");
  const [metricField, setMetricField] = useState("");
  const [metricOperation, setMetricOperation] =
    useState<"count" | "sum" | "avg" | "min" | "max">("sum");
  const [working, setWorking] = useState(false);

  const selectedDataset =
    datasets.find((dataset) => dataset.id === datasetId) || datasets[0];
  const numericColumns =
    selectedDataset?.columns.filter((column) =>
      ["integer", "number"].includes(column.type),
    ) || [];
  const selectedGroup =
    groupBy && selectedDataset?.columns.some((column) => column.name === groupBy)
      ? groupBy
      : selectedDataset?.columns[0]?.name || "";
  const selectedMetric =
    metricField && numericColumns.some((column) => column.name === metricField)
      ? metricField
      : numericColumns[0]?.name || "";

  async function submitDataset(event: FormEvent) {
    event.preventDefault();
    const chosenSource = sourceId || tabularSources[0]?.id;
    if (!chosenSource || !datasetName.trim()) return;
    setWorking(true);
    try {
      await createDataset(chosenSource, datasetName.trim(), datasetDescription.trim());
      setDatasetName("");
      setDatasetDescription("");
    } finally {
      setWorking(false);
    }
  }

  async function submitDashboard(event: FormEvent) {
    event.preventDefault();
    if (!selectedDataset || !dashboardName.trim()) return;
    const operation = selectedMetric ? metricOperation : "count";
    const label =
      operation === "count" ? "count" : `${operation}_${selectedMetric}`;
    const metric =
      operation === "count"
        ? { field: null, operation: "count" as const, label }
        : { field: selectedMetric, operation, label };
    setWorking(true);
    try {
      await createDashboard({
        name: dashboardName.trim(),
        dataset_id: selectedDataset.id,
        spec: {
          visualization,
          query: {
            group_by: selectedGroup || null,
            metrics: [metric],
            order_by: label,
            order_direction: "desc",
            limit: 100,
          },
          x_field: selectedGroup || label,
          y_fields: [label],
        },
      });
      setDashboardName("");
    } finally {
      setWorking(false);
    }
  }

  return (
    <section className="content-page analytics-page">
      <div className="page-heading">
        <div>
          <h1>Dashboards</h1>
          <p>Versioned CSV and JSON datasets with bounded, typed queries.</p>
        </div>
      </div>

      <div className="builder-grid">
        <form className="builder-card" onSubmit={(event) => void submitDataset(event)}>
          <div className="builder-title">
            <Database size={17} />
            <strong>New dataset</strong>
          </div>
          <label>
            Source
            <select
              value={sourceId || tabularSources[0]?.id || ""}
              onChange={(event) => setSourceId(event.target.value)}
              disabled={tabularSources.length === 0}
            >
              {tabularSources.map((source) => (
                <option value={source.id} key={source.id}>
                  {source.filename}
                </option>
              ))}
            </select>
          </label>
          <label>
            Name
            <input
              value={datasetName}
              onChange={(event) => setDatasetName(event.target.value)}
              placeholder="Revenue"
              maxLength={160}
            />
          </label>
          <label>
            Description
            <input
              value={datasetDescription}
              onChange={(event) => setDatasetDescription(event.target.value)}
              placeholder="Optional"
              maxLength={500}
            />
          </label>
          <button
            className="primary-button"
            type="submit"
            disabled={!tabularSources.length || !datasetName.trim() || working}
          >
            Create dataset
          </button>
        </form>

        <form className="builder-card" onSubmit={(event) => void submitDashboard(event)}>
          <div className="builder-title">
            <BarChart3 size={17} />
            <strong>New dashboard</strong>
          </div>
          <label>
            Dataset
            <select
              value={selectedDataset?.id || ""}
              onChange={(event) => {
                setDatasetId(event.target.value);
                setGroupBy("");
                setMetricField("");
              }}
              disabled={datasets.length === 0}
            >
              {datasets.map((dataset) => (
                <option value={dataset.id} key={dataset.id}>
                  {dataset.name} · v{dataset.current_version}
                </option>
              ))}
            </select>
          </label>
          <label>
            Name
            <input
              value={dashboardName}
              onChange={(event) => setDashboardName(event.target.value)}
              placeholder="Revenue by team"
              maxLength={160}
            />
          </label>
          <div className="field-pair">
            <label>
              Group
              <select
                value={selectedGroup}
                onChange={(event) => setGroupBy(event.target.value)}
              >
                {selectedDataset?.columns.map((column) => (
                  <option value={column.name} key={column.name}>
                    {column.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Chart
              <select
                value={visualization}
                onChange={(event) =>
                  setVisualization(event.target.value as DashboardSpec["visualization"])
                }
              >
                <option value="bar">Bar</option>
                <option value="line">Line</option>
                <option value="donut">Donut</option>
                <option value="table">Table</option>
              </select>
            </label>
          </div>
          <div className="field-pair">
            <label>
              Metric
              <select
                value={selectedMetric}
                onChange={(event) => setMetricField(event.target.value)}
                disabled={numericColumns.length === 0}
              >
                {numericColumns.map((column) => (
                  <option value={column.name} key={column.name}>
                    {column.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Operation
              <select
                value={numericColumns.length ? metricOperation : "count"}
                onChange={(event) =>
                  setMetricOperation(
                    event.target.value as "count" | "sum" | "avg" | "min" | "max",
                  )
                }
              >
                <option value="sum">Sum</option>
                <option value="avg">Average</option>
                <option value="min">Minimum</option>
                <option value="max">Maximum</option>
                <option value="count">Count</option>
              </select>
            </label>
          </div>
          <button
            className="primary-button"
            type="submit"
            disabled={!selectedDataset || !dashboardName.trim() || working}
          >
            Create dashboard
          </button>
        </form>
      </div>

      <div className="dataset-strip">
        {datasets.length === 0 ? (
          <span>No datasets yet.</span>
        ) : (
          datasets.map((dataset) => (
            <div key={dataset.id}>
              <Database size={15} />
              <strong>{dataset.name}</strong>
              <span>
                {dataset.row_count.toLocaleString()} rows · {dataset.columns.length} columns · v
                {dataset.current_version}
              </span>
              {tabularSources[0] && (
                <button
                  title={`Create a new version from ${tabularSources[0].filename}`}
                  onClick={() =>
                    void createDatasetVersion(
                      dataset.id,
                      sourceId || tabularSources[0].id,
                    )
                  }
                >
                  New version
                </button>
              )}
            </div>
          ))
        )}
      </div>

      <div className="dashboard-grid">
        {dashboards.length === 0 ? (
          <div className="feature-empty">
            <BarChart3 size={24} />
            <strong>No dashboards yet</strong>
            <span>Create a dataset, then define a bounded aggregation.</span>
          </div>
        ) : (
          dashboards.map((dashboard) => (
            <article className="dashboard-card" key={dashboard.id}>
              <div className="dashboard-card-head">
                <div>
                  <strong>{dashboard.name}</strong>
                  <span>{dashboard.spec.visualization} · {formatRelative(dashboard.updated_at)}</span>
                </div>
                <button onClick={() => void runDashboard(dashboard.id)}>
                  <RefreshCw size={14} />
                  Run
                </button>
              </div>
              {runs[dashboard.id] ? (
                <DashboardResultView run={runs[dashboard.id]} />
              ) : (
                <button
                  className="dashboard-run-empty"
                  onClick={() => void runDashboard(dashboard.id)}
                >
                  Run dashboard
                </button>
              )}
            </article>
          ))
        )}
      </div>
    </section>
  );
}

function DashboardResultView({ run }: { run: DashboardRun }) {
  const { result } = run;
  const xField = run.dashboard.spec.x_field || result.columns[0];
  const yField = run.dashboard.spec.y_fields[0] || result.columns[1];
  const numericValues = result.rows.map((row) => Number(row[yField] || 0));
  const maximum = Math.max(...numericValues, 1);

  if (run.dashboard.spec.visualization === "table") {
    return (
      <div className="result-table-wrap">
        <table className="result-table">
          <thead>
            <tr>
              {result.columns.map((column) => (
                <th key={column}>{column}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {result.rows.slice(0, 12).map((row, index) => (
              <tr key={index}>
                {result.columns.map((column) => (
                  <td key={column}>{String(row[column] ?? "—")}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }

  return (
    <div className={`mini-chart ${run.dashboard.spec.visualization}`}>
      {result.rows.slice(0, 12).map((row, index) => {
        const value = numericValues[index] || 0;
        return (
          <div className="mini-bar-row" key={index}>
            <span>{String(row[xField] ?? "—")}</span>
            <div>
              <i style={{ width: `${Math.max(2, (value / maximum) * 100)}%` }} />
            </div>
            <strong>{value.toLocaleString()}</strong>
          </div>
        );
      })}
      <small>
        {result.row_count} rows · {result.elapsed_ms.toFixed(1)} ms
        {result.truncated ? " · truncated" : ""}
      </small>
    </div>
  );
}

type AppsViewProps = {
  apps: GeneratedApp[];
  dashboards: Dashboard[];
  createApp: (payload: {
    name: string;
    slug: string;
    description?: string;
    visibility: "private" | "public";
    app_type?: "dashboard" | "code";
    dashboard_ids: string[];
  }) => Promise<void>;
  createRelease: (app: GeneratedApp, dashboardIds: string[]) => Promise<void>;
  publish: (app: GeneratedApp, releaseId: string) => Promise<void>;
  rollback: (app: GeneratedApp, releaseId: string) => Promise<void>;
  preview: (app: GeneratedApp) => Promise<void>;
  openStudio: (app: GeneratedApp) => void;
};

function AppsView({
  apps,
  dashboards,
  createApp,
  createRelease,
  publish,
  rollback,
  preview,
  openStudio,
}: AppsViewProps) {
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [visibility, setVisibility] = useState<"private" | "public">("private");
  const [appType, setAppType] = useState<"dashboard" | "code">("dashboard");
  const [selected, setSelected] = useState<string[]>([]);
  const [working, setWorking] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const dashboardIds =
      appType === "code"
        ? []
        : selected.length
          ? selected
          : dashboards.slice(0, 1).map((item) => item.id);
    if (!name.trim() || !slug.trim()) return;
    if (appType === "dashboard" && dashboardIds.length === 0) return;
    setWorking(true);
    try {
      await createApp({
        name: name.trim(),
        slug: slug.trim().toLowerCase(),
        visibility,
        app_type: appType,
        dashboard_ids: dashboardIds,
      });
      setName("");
      setSlug("");
      setSelected([]);
    } finally {
      setWorking(false);
    }
  }

  return (
    <section className="content-page apps-page">
      <div className="page-heading">
        <div>
          <h1>Apps</h1>
          <p>Immutable dashboard snapshots with explicit publish and rollback.</p>
        </div>
      </div>

      <form className="app-builder" onSubmit={(event) => void submit(event)}>
        <div className="builder-title">
          <AppWindow size={17} />
          <strong>New app</strong>
        </div>
        <div className="app-builder-fields">
          <label>
            Name
            <input
              value={name}
              onChange={(event) => {
                setName(event.target.value);
                if (!slug) {
                  setSlug(
                    event.target.value
                      .toLowerCase()
                      .replace(/[^a-z0-9]+/g, "-")
                      .replace(/^-|-$/g, ""),
                  );
                }
              }}
              placeholder="Operations brief"
              maxLength={160}
            />
          </label>
          <label>
            Slug
            <input
              value={slug}
              onChange={(event) => setSlug(event.target.value)}
              placeholder="operations-brief"
              pattern="[a-z0-9]+(?:-[a-z0-9]+)*"
              minLength={3}
              maxLength={80}
            />
          </label>
          <label>
            Visibility
            <select
              value={visibility}
              onChange={(event) =>
                setVisibility(event.target.value as "private" | "public")
              }
            >
              <option value="private">Workspace only</option>
              <option value="public">Public link</option>
            </select>
          </label>
          <label>
            Type
            <select
              value={appType}
              onChange={(event) =>
                setAppType(event.target.value as "dashboard" | "code")
              }
            >
              <option value="dashboard">Dashboard snapshot</option>
              <option value="code">Coded app (AI-generated)</option>
            </select>
          </label>
        </div>
        {appType === "dashboard" ? (
          <fieldset className="dashboard-picker">
            <legend>Dashboards</legend>
            {dashboards.map((dashboard) => (
              <label key={dashboard.id}>
                <input
                  type="checkbox"
                  checked={selected.includes(dashboard.id)}
                  onChange={(event) =>
                    setSelected((items) =>
                      event.target.checked
                        ? [...items, dashboard.id]
                        : items.filter((id) => id !== dashboard.id),
                    )
                  }
                />
                {dashboard.name}
              </label>
            ))}
            {dashboards.length === 0 && <span>Create a dashboard first.</span>}
          </fieldset>
        ) : (
          <p className="app-builder-hint">
            Coded apps are generated from a prompt in the Studio and run in a
            sandboxed frame with no network access. Create the app, then open
            its Studio to generate.
          </p>
        )}
        <button
          className="primary-button"
          type="submit"
          disabled={
            !name.trim() ||
            !slug.trim() ||
            (appType === "dashboard" && dashboards.length === 0) ||
            working
          }
        >
          {appType === "code" ? "Create coded app" : "Create draft"}
        </button>
      </form>

      <div className="app-list">
        {apps.length === 0 ? (
          <div className="feature-empty">
            <AppWindow size={24} />
            <strong>No apps yet</strong>
            <span>Package one or more dashboards into a static release.</span>
          </div>
        ) : (
          apps.map((app) => {
            const current =
              app.releases.find((release) => release.id === app.current_release_id) ||
              app.releases[0];
            const latest = app.releases[0];
            return (
              <article className="app-card" key={app.id}>
                <div className="app-card-head">
                  <div className="app-icon">
                    {app.visibility === "public" ? <Globe2 size={18} /> : <LockKeyhole size={18} />}
                  </div>
                  <div>
                    <strong>{app.name}</strong>
                    <span>
                      /apps/{app.slug} · {app.visibility}
                    </span>
                  </div>
                  <span className={`release-status ${current?.status || "draft"}`}>
                    {current?.status || "draft"}
                  </span>
                </div>

                {current && (
                  <div className="app-snapshot">
                    <div>
                      <strong>v{current.version}</strong>
                      <span>
                        {current.manifest.kind === "code"
                          ? "sandboxed coded app"
                          : `${current.manifest.dashboards?.length || 0} dashboards`}
                      </span>
                    </div>
                    <span>{current.content_hash.slice(0, 12)}</span>
                  </div>
                )}

                <div className="app-actions">
                  {app.app_type === "code" ? (
                    <button onClick={() => openStudio(app)}>Studio</button>
                  ) : (
                    <button
                      onClick={() =>
                        void createRelease(
                          app,
                          dashboards.map((dashboard) => dashboard.id),
                        )
                      }
                      disabled={dashboards.length === 0}
                    >
                      New snapshot
                    </button>
                  )}
                  {app.app_type !== "code" && app.releases.length > 0 && (
                    <button onClick={() => void preview(app)}>Preview</button>
                  )}
                  {latest?.status === "draft" && (
                    <button
                      className="publish-button"
                      onClick={() => void publish(app, latest.id)}
                    >
                      Publish v{latest.version}
                    </button>
                  )}
                  {app.visibility === "public" && app.current_release_id && (
                    <a href={`/apps/${app.slug}`} target="_blank" rel="noreferrer">
                      Open
                    </a>
                  )}
                </div>

                {app.releases.length > 1 && (
                  <div className="release-list">
                    {app.releases.map((release) => (
                      <div key={release.id}>
                        <span>
                          v{release.version} · {release.status}
                        </span>
                        {release.id !== app.current_release_id &&
                          release.status !== "draft" && (
                            <button onClick={() => void rollback(app, release.id)}>
                              Roll back
                            </button>
                          )}
                      </div>
                    ))}
                  </div>
                )}
              </article>
            );
          })
        )}
      </div>
    </section>
  );
}

type AppStudioProps = {
  app: GeneratedApp | null;
  datasets: Dataset[];
  generate: (app: GeneratedApp, prompt: string, datasetIds: string[]) => Promise<void>;
  publish: (app: GeneratedApp, releaseId: string) => Promise<void>;
  onClose: () => void;
  setError: (message: string) => void;
};

function AppStudio({ app, datasets, generate, publish, onClose, setError }: AppStudioProps) {
  const [prompt, setPrompt] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [working, setWorking] = useState(false);
  const [frameNonce, setFrameNonce] = useState(0);

  if (!app) return null;
  const latest = app.releases[0];
  const codeRelease = latest?.manifest.kind === "code" ? latest : null;

  async function runGenerate() {
    if (!app || !prompt.trim()) return;
    setWorking(true);
    try {
      await generate(app, prompt.trim(), selected);
      setFrameNonce((value) => value + 1);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Generation failed");
    } finally {
      setWorking(false);
    }
  }

  return (
    <div className="drawer-scrim" onClick={onClose}>
      <aside className="app-studio" onClick={(event) => event.stopPropagation()}>
        <div className="drawer-header">
          <div>
            <span>App Studio · sandboxed</span>
            <strong>{app.name}</strong>
          </div>
          <button className="icon-button" onClick={onClose} aria-label="Close studio">
            <X size={18} />
          </button>
        </div>

        <div className="studio-body">
          <div className="studio-controls">
            <label>
              What should this app show?
              <textarea
                value={prompt}
                onChange={(event) => setPrompt(event.target.value)}
                placeholder={
                  codeRelease
                    ? "Describe a change; the current app is used as the starting point…"
                    : "e.g. A training dashboard with weekly distance and a highlights table"
                }
                rows={3}
                maxLength={4000}
              />
            </label>
            <fieldset className="dashboard-picker">
              <legend>Datasets the app may use</legend>
              {datasets.map((dataset) => (
                <label key={dataset.id}>
                  <input
                    type="checkbox"
                    checked={selected.includes(dataset.id)}
                    onChange={(event) =>
                      setSelected((items) =>
                        event.target.checked
                          ? [...items, dataset.id]
                          : items.filter((id) => id !== dataset.id),
                      )
                    }
                  />
                  {dataset.name}
                </label>
              ))}
              {datasets.length === 0 && (
                <span>No datasets yet — the app will be static.</span>
              )}
            </fieldset>
            <div className="studio-actions">
              <button
                className="primary-button"
                onClick={() => void runGenerate()}
                disabled={!prompt.trim() || working}
              >
                {working
                  ? "Generating…"
                  : codeRelease
                    ? `Generate v${(latest?.version || 0) + 1}`
                    : "Generate"}
              </button>
              {codeRelease && codeRelease.status === "draft" && (
                <button
                  className="publish-button"
                  onClick={() => void publish(app, codeRelease.id)}
                >
                  Publish v{codeRelease.version}
                </button>
              )}
            </div>
            <p className="studio-note">
              <ShieldCheck size={13} /> Generated code runs in a sandboxed frame:
              no network, no cookies, no page access. Data flows only through the
              typed dataset query API.
            </p>
          </div>

          <div className="studio-preview">
            {codeRelease ? (
              <SandboxFrame
                key={`${codeRelease.id}-${frameNonce}`}
                src={api.frameUrl(app.id, codeRelease.id)}
                title={`${app.name} preview`}
                snapshots={codeRelease.manifest.snapshots || {}}
                bindings={codeRelease.manifest.data_bindings || []}
                api={api}
                className="sandbox-frame"
              />
            ) : (
              <div className="feature-empty">
                <AppWindow size={22} />
                <strong>No version yet</strong>
                <span>Describe the app and generate a first version.</span>
              </div>
            )}
          </div>
        </div>
      </aside>
    </div>
  );
}

const PROVIDER_LABELS: Record<string, { name: string; blurb: string }> = {
  google: {
    name: "Google (Gmail)",
    blurb: "Read-only Gmail sync. Emails become searchable sources and a metadata dataset.",
  },
  strava: {
    name: "Strava",
    blurb:
      "OAuth activity sync. Activities become a queryable dataset for chat and dashboards.",
  },
  garmin: {
    name: "Garmin Connect",
    blurb:
      "Credential-based sync via the unofficial Garmin Connect API (python-garminconnect). " +
      "Your password is used once to log in and is never stored — only an encrypted session token.",
  },
};

type IntegrationsViewProps = {
  integrations: IntegrationProvider[];
  connect: (provider: string) => Promise<void>;
  connectGarmin: (email: string, password: string) => Promise<void>;
  disconnect: (accountId: string) => Promise<void>;
  sync: (accountId: string) => Promise<void>;
  setError: (message: string) => void;
};

function IntegrationsView({
  integrations,
  connect,
  connectGarmin,
  disconnect,
  sync,
  setError,
}: IntegrationsViewProps) {
  const [syncing, setSyncing] = useState<string | null>(null);
  const [garminEmail, setGarminEmail] = useState("");
  const [garminPassword, setGarminPassword] = useState("");
  const [garminWorking, setGarminWorking] = useState(false);

  async function submitGarmin(event: FormEvent) {
    event.preventDefault();
    if (!garminEmail.trim() || !garminPassword) return;
    setGarminWorking(true);
    try {
      await connectGarmin(garminEmail.trim(), garminPassword);
      setGarminEmail("");
      setGarminPassword("");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Garmin login failed");
    } finally {
      setGarminWorking(false);
    }
  }

  async function runSync(accountId: string) {
    setSyncing(accountId);
    try {
      await sync(accountId);
    } finally {
      setSyncing(null);
    }
  }

  return (
    <section className="content-page integrations-page">
      <div className="page-heading">
        <div>
          <h1>Integrations</h1>
          <p>
            Connect external accounts. Synced data lands as sources and datasets, and the
            assistant gets read-only tools for live lookups.
          </p>
        </div>
        <div className="security-badge">
          <ShieldCheck size={16} />
          Tokens encrypted at rest
        </div>
      </div>

      <div className="integration-grid">
        {integrations.map((item) => {
          const label = PROVIDER_LABELS[item.provider] || {
            name: item.provider,
            blurb: "",
          };
          return (
            <article className="integration-card" key={item.provider}>
              <div className="integration-card-head">
                <div className="integration-icon">
                  <Plug size={18} />
                </div>
                <div>
                  <strong>{label.name}</strong>
                  <span>
                    {item.account
                      ? `${item.account.external_account || "connected"} · ${item.account.status}`
                      : item.configured
                        ? "Not connected"
                        : "Not configured"}
                  </span>
                </div>
              </div>
              <p>{label.blurb}</p>
              {!item.configured && !item.account && (
                <small>
                  Set the client credentials and INTEGRATIONS_ENCRYPTION_KEY in{" "}
                  <code>.env</code> to enable this provider.
                </small>
              )}
              <div className="integration-actions">
                {item.account ? (
                  <>
                    <button
                      className="primary-button"
                      onClick={() => void runSync(item.account!.id)}
                      disabled={syncing === item.account.id}
                    >
                      <RefreshCw
                        size={14}
                        className={syncing === item.account.id ? "spin" : ""}
                      />
                      {syncing === item.account.id ? "Syncing…" : "Sync now"}
                    </button>
                    <button onClick={() => void disconnect(item.account!.id)}>
                      Disconnect
                    </button>
                    {item.account.last_sync_at && (
                      <span>synced {formatRelative(item.account.last_sync_at)}</span>
                    )}
                  </>
                ) : item.provider === "garmin" ? (
                  <form className="garmin-form" onSubmit={(event) => void submitGarmin(event)}>
                    <input
                      type="email"
                      value={garminEmail}
                      onChange={(event) => setGarminEmail(event.target.value)}
                      placeholder="Garmin account email"
                      autoComplete="off"
                      disabled={!item.configured}
                    />
                    <input
                      type="password"
                      value={garminPassword}
                      onChange={(event) => setGarminPassword(event.target.value)}
                      placeholder="Password"
                      autoComplete="off"
                      disabled={!item.configured}
                    />
                    <button
                      className="primary-button"
                      type="submit"
                      disabled={
                        !item.configured ||
                        !garminEmail.trim() ||
                        !garminPassword ||
                        garminWorking
                      }
                    >
                      {garminWorking ? "Logging in…" : "Connect"}
                    </button>
                  </form>
                ) : (
                  <button
                    className="primary-button"
                    onClick={() => void connect(item.provider)}
                    disabled={!item.configured}
                  >
                    Connect
                  </button>
                )}
              </div>
            </article>
          );
        })}
      </div>

      <div className="integration-note">
        <ShieldCheck size={14} />
        Garmin uses the unofficial Connect API (no partner program), so it can break if
        Garmin changes things and does not support MFA-protected accounts yet. Garmin
        devices that auto-sync to Strava are also covered by the Strava integration.
      </div>
    </section>
  );
}

type ActivityViewProps = {
  calls: ToolCall[];
  events: AuditEvent[];
  decide: (call: ToolCall, decision: "approved" | "denied") => Promise<void>;
  activeRun: string | null;
};

function ActivityView({ calls, events, decide, activeRun }: ActivityViewProps) {
  const pending = calls.filter((call) => call.status === "proposed");
  return (
    <section className="content-page activity-page">
      <div className="page-heading">
        <div>
          <h1>Activity</h1>
          <p>Review tool requests and recorded actions.</p>
        </div>
        <div className="security-badge">
          <ShieldCheck size={16} />
          Read-only tools
        </div>
      </div>

      <div className="activity-grid">
        <div className="approval-panel">
          <div className="panel-title">
            <div>
              <strong>Pending approvals</strong>
            </div>
            <span className="panel-count">{pending.length}</span>
          </div>
          {pending.length === 0 ? (
            <div className="approval-empty">
              <div>
                <Check size={18} />
              </div>
              <strong>No pending requests</strong>
              <p>Tool calls requiring approval appear here.</p>
              <code>/tool github-zen</code>
            </div>
          ) : (
            pending.map((call) => (
              <article className="approval-card" key={call.id}>
                <div className="approval-card-top">
                  <div className="tool-glyph">
                    <Activity size={17} />
                  </div>
                  <div>
                    <span>Assistant request</span>
                    <strong>{call.tool_name}</strong>
                  </div>
                  <span className="method-pill">GET</span>
                </div>
                <div className="request-url">{call.request_url}</div>
                <div className="safety-note">
                  <ShieldCheck size={14} />
                  HTTPS, allowlisted host, 10-second timeout, 256 KB cap
                </div>
                <div className="decision-buttons">
                  <button onClick={() => void decide(call, "denied")}>
                    <X size={15} />
                    Deny
                  </button>
                  <button
                    className="approve"
                    onClick={() => void decide(call, "approved")}
                  >
                    <Check size={15} />
                    Approve once
                  </button>
                </div>
              </article>
            ))
          )}
        </div>

        <div className="audit-panel">
          <div className="panel-title">
            <div>
              <strong>Audit log</strong>
            </div>
            {activeRun && <span className="live-pill">Live</span>}
          </div>
          <div className="timeline">
            {events.length === 0 ? (
              <p className="timeline-empty">Activity will appear as you use the workspace.</p>
            ) : (
              events.map((event) => (
                <div className="timeline-event" key={event.id}>
                  <div className="timeline-dot" />
                  <div>
                    <strong>{event.action.replaceAll(".", " ")}</strong>
                    <span>
                      {event.resource_type} · {formatRelative(event.created_at)}
                    </span>
                    {Object.keys(event.detail).length > 0 && (
                      <small>
                        {Object.entries(event.detail)
                          .slice(0, 2)
                          .map(([key, value]) => `${key}: ${String(value)}`)
                          .join(" · ")}
                      </small>
                    )}
                  </div>
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
