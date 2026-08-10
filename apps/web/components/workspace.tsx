"use client";

import { CircleDot, LogOut, Menu, Plus, Trash2, X } from "lucide-react";
import { useEffect, useState } from "react";
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
import { MemoryView } from "./views/memory";
import {
  DEFAULT_GROUP_VIEW,
  NAV_GROUPS,
  groupForView,
  type GroupId,
} from "./views/navigation";
import { ProjectsView } from "./views/projects";
import { SandboxView } from "./views/sandbox";
import { PAGE_TITLES, formatRelative, type View } from "./views/shared";
import { SourcesView } from "./views/sources";
import { ThemeToggle } from "./theme-toggle";

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

  // Per-view badge numbers. A view with no count (Chat, Graph, Activity) is
  // absent: Graph's size is a projection of Sources, so counting it in the
  // Knowledge badge would double-count what the user actually put in.
  const viewCounts: Partial<Record<View, number>> = {
    sources: sources.length,
    memory: memories.length,
    documents: documents.length,
    projects: projects.length,
    boards: boards.length,
    dashboards: dashboards.length,
    data: dbConnections.length,
    mcp: mcpServers.length,
    integrations: integrations.filter((item) => item.account).length,
  };

  const activeGroup = groupForView(view);
  // A one-view group gets no strip: a tab bar with a single tab is noise.
  const hasTabs = activeGroup.items.length > 1;

  // Where each group reopens. Tracking `view` rather than the click means an
  // action that navigates on its own — an upload landing on Sources, the OAuth
  // return landing on Integrations — is remembered too.
  const [groupHome, setGroupHome] = useState<Record<GroupId, View>>(DEFAULT_GROUP_VIEW);
  useEffect(() => {
    const group = groupForView(view).id;
    setGroupHome((current) =>
      current[group] === view ? current : { ...current, [group]: view },
    );
  }, [view]);

  function openGroup(groupId: GroupId) {
    setView(groupHome[groupId]);
    setSidebarOpen(false);
  }

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
          {NAV_GROUPS.map((group) => {
            const Icon = group.icon;
            const total = group.items.reduce(
              (sum, item) => sum + (viewCounts[item.view] ?? 0),
              0,
            );
            const counted = group.items.some((item) => viewCounts[item.view] !== undefined);
            return (
              <button
                key={group.id}
                className={activeGroup.id === group.id ? "nav-item active" : "nav-item"}
                aria-current={activeGroup.id === group.id ? "page" : undefined}
                onClick={() => openGroup(group.id)}
              >
                <Icon size={17} />
                {group.label}
                {counted && <span className="nav-count">{total}</span>}
                {group.id === "activity" && pendingApprovals.length > 0 && (
                  <span className="approval-count">{pendingApprovals.length}</span>
                )}
              </button>
            );
          })}
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

      <main className={hasTabs ? "main-panel has-tabs" : "main-panel"}>
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
            {hasTabs && <span>{activeGroup.label}</span>}
            <strong>{view === "chat" ? activeTitle : PAGE_TITLES[view]}</strong>
          </div>
          <div className="topbar-actions">
            <ThemeToggle />
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

        {hasTabs && (
          <nav className="view-tabs" aria-label={`${activeGroup.label} views`}>
            {activeGroup.items.map((item) => {
              const Icon = item.icon;
              const count = viewCounts[item.view];
              return (
                <button
                  key={item.view}
                  className={view === item.view ? "view-tab active" : "view-tab"}
                  aria-current={view === item.view ? "page" : undefined}
                  onClick={() => setView(item.view)}
                >
                  <Icon size={14} />
                  {item.label}
                  {count !== undefined && <span className="tab-count">{count}</span>}
                </button>
              );
            })}
          </nav>
        )}

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

        {view === "memory" && (
          <MemoryView memories={memories} forgetMemory={forgetMemory} />
        )}

        {view === "graph" && (
          <GraphView graph={graph} rebuild={rebuildKnowledgeGraph} openChunk={openChunk} />
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

        {/* Self-contained: sandboxes are metered by the minute, so they are
            fetched when this view opens rather than with the workspace. */}
        {view === "sandbox" && <SandboxView setError={setError} />}

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
