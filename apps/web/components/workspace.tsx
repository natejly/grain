"use client";

import {
  Activity,
  BarChart3,
  Blocks,
  Braces,
  CircleDot,
  Database,
  FileText,
  KanbanSquare,
  Library,
  LogOut,
  Menu,
  MessageSquare,
  Network,
  Plug,
  Plus,
  Trash2,
  X,
} from "lucide-react";
import { api } from "./api";
import { ApiHealthBanner } from "./api-health-banner";
import { useSession } from "./auth/session-provider";
import { useWorkspace } from "./use-workspace";
import { ActivityView } from "./views/activity";
import { BoardView } from "./views/board";
import { ChatView } from "./views/chat";
import { DashboardEditor } from "./views/dashboard-editor";
import { DashboardsView } from "./views/dashboards";
import { DataView } from "./views/data";
import { DocumentsView } from "./views/documents";
import { GraphView } from "./views/graph";
import { IntegrationsView } from "./views/integrations";
import { McpView } from "./views/mcp";
import { ProjectsView } from "./views/projects";
import { PAGE_TITLES, formatRelative } from "./views/shared";
import { SourcesView } from "./views/sources";

export function Workspace() {
  const {
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
    openDocument,
    createDocument,
    saveDocument,
    restoreDocumentVersion,
    removeDocument,
    decidePendingEdit,
    createBoard,
    addBoardCard,
    moveBoardCard,
    removeBoardCard,
    removeBoard,
    boardColumnOps,
    addDbConnection,
    testDbConnection,
    removeDbConnection,
    loadDbSchema,
    openProject,
    createProject,
    saveProjectFile,
    removeProjectFile,
    removeProject,
    addMcpServer,
    refreshMcpServer,
    setMcpServerEnabled,
    setMcpToolEnabled,
    removeMcpServer,
    selectConversation,
    newConversation,
    removeConversation,
    decideAgentCall,
    cancelActiveRun,
    regenerate,
    submitPrompt,
    decide,
    uploadFiles,
    removeSource,
    openChunk,
    openCitation,
    rebuildKnowledgeGraph,
    forgetMemory,
    createDashboard,
    generateDashboard,
    publishGeneratedApp,
    rollbackGeneratedApp,
    connectIntegration,
    connectGarminAccount,
    disconnectIntegration,
    syncIntegration,
  } = useWorkspace();
  // Always present: this component only renders inside the authenticated gate.
  const { session, signOut } = useSession();

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
            className={view === "dashboards" ? "nav-item active" : "nav-item"}
            onClick={() => {
              setView("dashboards");
              setSidebarOpen(false);
            }}
          >
            <BarChart3 size={17} />
            Dashboards
            <span className="nav-count">{dashboards.length}</span>
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
            className={view === "documents" ? "nav-item active" : "nav-item"}
            onClick={() => {
              setView("documents");
              setSidebarOpen(false);
            }}
          >
            <FileText size={17} />
            Documents
            <span className="nav-count">{documents.length}</span>
          </button>
          <button
            className={view === "boards" ? "nav-item active" : "nav-item"}
            onClick={() => {
              setView("boards");
              setSidebarOpen(false);
            }}
          >
            <KanbanSquare size={17} />
            Boards
            <span className="nav-count">{boards.length}</span>
          </button>
          <button
            className={view === "data" ? "nav-item active" : "nav-item"}
            onClick={() => {
              setView("data");
              setSidebarOpen(false);
            }}
          >
            <Database size={17} />
            Databases
            <span className="nav-count">{dbConnections.length}</span>
          </button>
          <button
            className={view === "projects" ? "nav-item active" : "nav-item"}
            onClick={() => {
              setView("projects");
              setSidebarOpen(false);
            }}
          >
            <Braces size={17} />
            Projects
            <span className="nav-count">{projects.length}</span>
          </button>
          <button
            className={view === "mcp" ? "nav-item active" : "nav-item"}
            onClick={() => {
              setView("mcp");
              setSidebarOpen(false);
            }}
          >
            <Blocks size={17} />
            MCP
            <span className="nav-count">{mcpServers.length}</span>
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
            {(session?.user_name || "U").slice(0, 1).toUpperCase()}
          </div>
          <div>
            <strong>{session?.user_name || "Connecting…"}</strong>
            <span>{session?.user_email || session?.workspace_name || ""}</span>
          </div>
          <button
            className="icon-button"
            onClick={() => void signOut()}
            title="Sign out"
            aria-label="Sign out"
          >
            <LogOut size={15} />
          </button>
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
            <strong>{view === "chat" ? activeTitle : PAGE_TITLES[view]}</strong>
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
            agentCalls={agentCalls}
            draft={draft}
            setDraft={setDraft}
            activeRun={activeRun}
            runStatus={runStatus}
            submitPrompt={submitPrompt}
            cancelActiveRun={cancelActiveRun}
            regenerate={regenerate}
            decideAgentCall={decideAgentCall}
            openCitation={openCitation}
            onAttach={() => setView("sources")}
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

        {view === "dashboards" && (
          <DashboardsView
            apps={dashboards}
            openEditor={setEditing}
            publish={publishGeneratedApp}
            rollback={rollbackGeneratedApp}
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

        {view === "documents" && (
          <DocumentsView
            documents={documents}
            active={activeDocument}
            versions={documentVersions}
            openDocument={openDocument}
            createDocument={createDocument}
            saveDocument={saveDocument}
            restoreVersion={restoreDocumentVersion}
            removeDocument={removeDocument}
            pendingEdits={pendingEdits}
            decidePendingEdit={decidePendingEdit}
          />
        )}

        {view === "boards" && (
          <BoardView
            boards={boards}
            createBoard={createBoard}
            addCard={addBoardCard}
            moveCard={moveBoardCard}
            removeCard={removeBoardCard}
            removeBoard={removeBoard}
            columnOps={boardColumnOps}
          />
        )}

        {view === "data" && (
          <DataView
            connections={dbConnections}
            addConnection={addDbConnection}
            testConnection={testDbConnection}
            removeConnection={removeDbConnection}
            loadSchema={loadDbSchema}
          />
        )}

        {view === "projects" && (
          <ProjectsView
            projects={projects}
            active={activeProject}
            openProject={openProject}
            createProject={createProject}
            saveFile={saveProjectFile}
            removeFile={removeProjectFile}
            removeProject={removeProject}
          />
        )}

        {view === "mcp" && (
          <McpView
            servers={mcpServers}
            addServer={addMcpServer}
            refreshServer={refreshMcpServer}
            setServerEnabled={setMcpServerEnabled}
            setToolEnabled={setMcpToolEnabled}
            removeServer={removeMcpServer}
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

      {editing && (
        <DashboardEditor
          app={editing === "new" ? null : apps.find((item) => item.id === editing) || null}
          datasets={datasets}
          create={createDashboard}
          generate={generateDashboard}
          publish={publishGeneratedApp}
          onCreated={setEditing}
          onClose={() => setEditing(null)}
          setError={setError}
        />
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
              </>
            )}
          </aside>
        </div>
      )}
    </div>
  );
}
