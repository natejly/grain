"use client";

import type { DocumentKind } from "@workspace/api-client";
import { BarChart3, CircleDot, LogOut, Menu, Plus, Trash2, X } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "./api";
import { ApiHealthBanner } from "./api-health-banner";
import { useSession } from "./auth/session-provider";
import { CreateMenu } from "./create-menu";
import { SettingsMenu } from "./settings-menu";
import { useWorkspace } from "./use-workspace";
import { ActivityView } from "./views/activity";
import { AdminView } from "./views/admin";
import { AgentsView } from "./views/agents";
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
  RAIL_GROUPS,
  groupForView,
  type CreateAction,
  type GroupId,
} from "./views/navigation";
import { ProjectsView } from "./views/projects";
import { PAGE_TITLES, formatRelative, type View } from "./views/shared";
import { SourcesView } from "./views/sources";
import { TodosView } from "./views/todos";
import { ThemeToggle } from "./theme-toggle";
import { WorkflowsView } from "./views/workflows";
import { WorkspaceSwitcher } from "./workspace-selection";

export function Workspace() {
  const {
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
    pendingEdits,
    dbConnections,
    projects,
    activeProject,
    view,
    setView,
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
    refreshPendingEdits,
    reloadOpenDocument,
    openDocument,
    createDocument,
    saveDocument,
    restoreDocumentVersion,
    removeDocument,
    decidePendingEdit,
    createFolder,
    renameFolder,
    moveFolder,
    removeFolder,
    moveDocument,
    createBoard,
    addBoardCard,
    moveBoardCard,
    removeBoardCard,
    removeBoard,
    boardColumnOps,
    kanbanBoards,
    todoLists,
    todoOps,
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
    setApprovalMode,
    cancelActiveRun,
    regenerate,
    submitPrompt,
    uploadFiles,
    removeSource,
    openChunk,
    openCitation,
    rebuildKnowledgeGraph,
    forgetMemory,
    createDashboard,
    generateDashboard,
    runDashboard,
    pinDashboard,
    unpinDashboard,
    saveDashboardLayout,
    bindDashboardTemplate,
    removeDashboard,
    publishGeneratedApp,
    rollbackGeneratedApp,
    connectIntegration,
    connectGarminAccount,
    disconnectIntegration,
    syncIntegration,
  } = useWorkspace();
  // Always present: this component only renders inside the authenticated gate.
  const { session, signOut } = useSession();

  // The open thread's own row, which is where its approval mode lives. Read
  // from the rail's list rather than held separately, so the picker and the
  // bypass indicator cannot disagree about which mode is in force.
  const activeThread = conversations.find((item) => item.id === activeConversation);
  const activeTitle = activeThread?.title || "New conversation";

  // Per-view badge numbers. A view with no count (Chat, Graph, Activity) is
  // absent: Graph's size is a projection of Sources, so counting it in the
  // Knowledge badge would double-count what the user actually put in.
  const viewCounts: Partial<Record<View, number>> = {
    sources: sources.length,
    memory: memories.length,
    documents: documents.length,
    projects: projects.length,
    // Each tab counts what that tab shows: a one-column board is on Lists,
    // not here, so counting it twice would make both numbers wrong.
    boards: kanbanBoards.length,
    todos: todoLists.length,
    // Both kinds live on that page, so the badge counts both; a number that
    // only counted half of what the page shows is worse than no number.
    dashboards: dashboardApps.length + dashboards.length,
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

  /** Totals for the groups the Settings menu hides; badges must not hide too. */
  const groupCounts: Partial<Record<GroupId, number>> = Object.fromEntries(
    NAV_GROUPS.filter((group) =>
      group.items.some((item) => viewCounts[item.view] !== undefined),
    ).map((group) => [
      group.id,
      group.items.reduce((sum, item) => sum + (viewCounts[item.view] ?? 0), 0),
    ]),
  );

  // "Workflow" in the Create menu opens the composer inside the panel that owns
  // workflows, rather than compiling anything on the way past. The panel lowers
  // the flag once it has acted, so navigating back later does not reopen a
  // composer the user dismissed.
  const [workflowRequested, setWorkflowRequested] = useState(false);

  /**
   * Make the thing, then show it — the whole point of moving Create off the
   * rail. The view is set *first*, synchronously: a `setView` after an awaited
   * create lands wherever the user has wandered to by the time it resolves.
   */
  async function create(action: CreateAction, name: string, kind: DocumentKind) {
    setView(action.view);
    setSidebarOpen(false);
    if (action.id === "document") return createDocument(name, kind);
    if (action.id === "project") return createProject(name, "");
    // The only Create entry that names a project *kind*: a LaTeX document is a
    // project seeded with a .tex that compiles to a PDF, which is what a user
    // picking "LaTeX" is after.
    if (action.id === "latex") return createProject(name, "", "latex");
    if (action.id === "board") return createBoard(name);
    // A workflow is a sentence, compiled and reviewed before it exists at all,
    // so the composer *is* the creation step — same as a dashboard's editor.
    if (action.id === "workflow") return setWorkflowRequested(true);
    // A dashboard is named, bound to data and generated in one editor; opening
    // it *is* the creation step, and it lands on the gallery when closed.
    return setEditing("new");
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

        {/* Above everything else because everything else is scoped to it. */}
        <WorkspaceSwitcher />

        <button className="chrome-button new-thread-button" onClick={newConversation}>
          <Plus size={16} />
          New thread
        </button>

        {/* The rail is the places you work. Creating is not one of them, and
            neither is administering — both moved to the top right. */}
        <nav className="primary-nav" aria-label="Workspace">
          {RAIL_GROUPS.map((group) => {
            const Icon = group.icon;
            const total = groupCounts[group.id];
            return (
              <button
                key={group.id}
                className={activeGroup.id === group.id ? "nav-item active" : "nav-item"}
                aria-current={activeGroup.id === group.id ? "page" : undefined}
                onClick={() => openGroup(group.id)}
              >
                <Icon size={17} />
                {group.label}
                {total !== undefined && <span className="nav-count">{total}</span>}
              </button>
            );
          })}
        </nav>

        {/* Beneath the destinations, and deliberately not among them: a pin is
            not a place the product has, it is a chart *this user* wanted where
            they could see it. Its own nav so a screen reader is told which is
            which, and so the rail's own list cannot grow by six on a Tuesday. */}
        {dashboardPins.length > 0 && (
          <nav className="pinned-nav" aria-label="Pinned dashboards">
            <span className="pinned-heading">Pinned</span>
            {dashboardPins.map((pin) => (
              <button
                key={pin.dashboard.id}
                className={
                  view === "dashboards" && focusedDashboard === pin.dashboard.id
                    ? "pinned-item active"
                    : "pinned-item"
                }
                onClick={() => {
                  setView("dashboards");
                  setSidebarOpen(false);
                  setFocusedDashboard(pin.dashboard.id);
                }}
              >
                <BarChart3 size={15} aria-hidden="true" />
                <span>{pin.dashboard.name}</span>
              </button>
            ))}
          </nav>
        )}

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
            <span>
              {session?.user_email || session?.workspace_name || ""}
              {/* The one place the verification state is legible. It is not a
                  sign-in requirement on purpose (gating login on delivered mail
                  turns an SMTP outage into a lockout with no way back in), but
                  it does decide whether a Google sign-in may claim this account
                  — apps/api/app/api/auth.py, the account-linking branch — so a
                  user is entitled to know which side of that line they are on. */}
              {session && session.user_email && !session.email_verified && (
                <span className="identity-unverified" title="Confirm your address from the email we sent">
                  unverified
                </span>
              )}
            </span>
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
            <CreateMenu create={create} />
            <SettingsMenu
              activeGroup={activeGroup.id}
              counts={groupCounts}
              approvals={pendingApprovals.length}
              open={openGroup}
            />
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
            apps={dashboardApps}
            draft={draft}
            setDraft={setDraft}
            activeRun={activeRun}
            runStatus={runStatus}
            budgetPark={budgetPark}
            submitPrompt={submitPrompt}
            cancelActiveRun={cancelActiveRun}
            regenerate={regenerate}
            decideAgentCall={decideAgentCall}
            openCitation={openCitation}
            onAttach={() => setView("sources")}
            approval={{
              mode: activeThread?.approval_mode ?? "ask_writes",
              setMode: setApprovalMode,
              conversationId: activeConversation,
              conversationTitle: activeTitle,
            }}
            todos={{ lists: todoLists, ops: todoOps }}
            endRef={endRef}
            selectedAgentId={selectedAgentId}
            onSelectAgent={setSelectedAgentId}
            turnControls={{
              models: bootstrap?.model_provider.selectable_models ?? [],
              efforts: bootstrap?.model_provider.reasoning_efforts ?? [],
              model: selectedModel,
              setModel: setSelectedModel,
              effort: selectedEffort,
              setEffort: setSelectedEffort,
              fast,
              setFast,
            }}
          />
        )}

        {view === "sources" && (
          <SourcesView
            sources={sources}
            setError={setError}
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
            apps={dashboardApps}
            openEditor={setEditing}
            publish={publishGeneratedApp}
            rollback={rollbackGeneratedApp}
            dashboards={dashboards}
            templates={dashboardTemplates}
            datasets={datasets}
            pins={dashboardPins}
            pinnedIds={pinnedIds}
            results={dashboardResults}
            runDashboard={runDashboard}
            pinDashboard={pinDashboard}
            unpinDashboard={unpinDashboard}
            saveDashboardLayout={saveDashboardLayout}
            bindDashboardTemplate={bindDashboardTemplate}
            removeDashboard={removeDashboard}
            focused={focusedDashboard}
            setFocused={setFocusedDashboard}
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
            folders={folders}
            folderOps={{
              createFolder,
              renameFolder,
              moveFolder,
              removeFolder,
              moveDocument,
            }}
            active={activeDocument}
            versions={documentVersions}
            openDocument={openDocument}
            createDocument={createDocument}
            saveDocument={saveDocument}
            restoreVersion={restoreDocumentVersion}
            removeDocument={removeDocument}
            pendingEdits={pendingEdits}
            decidePendingEdit={decidePendingEdit}
            chat={{
              agentId: bootstrap?.default_agent_id,
              sources,
              apps: dashboardApps,
              openCitation,
              // The panel's own run finished: re-read the document it was about
              // and the list its title appears in. The shell's other lists are
              // deliberately not refreshed from here — a thread scoped to one
              // document is not a reason to refetch six collections.
              reloadDocument: reloadOpenDocument,
              refreshPendingEdits,
            }}
          />
        )}

        {view === "boards" && (
          <BoardView
            boards={kanbanBoards}
            createBoard={createBoard}
            addCard={addBoardCard}
            moveCard={moveBoardCard}
            removeCard={removeBoardCard}
            removeBoard={removeBoard}
            columnOps={boardColumnOps}
          />
        )}

        {view === "todos" && <TodosView lists={todoLists} ops={todoOps} />}

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

        {/* Self-contained like WorkflowsView: the agent list is fetched when
            somebody opens the editor, not at page load. */}
        {view === "agents" && <AgentsView setError={setError} />}

        {/* Self-contained: a workflow's run history is nobody's business until
            they open this, so it is fetched here rather than at page load. */}
        {view === "workflows" && (
          <WorkflowsView
            setError={setError}
            composeRequested={workflowRequested}
            onComposeHandled={() => setWorkflowRequested(false)}
            onWorkspaceChanged={() => void refreshOffScreenWork().catch(() => undefined)}
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
            calls={agentCalls}
            events={auditEvents}
            decide={decideAgentCall}
            activeRun={activeRun}
          />
        )}

        {/* Owner-only and workspace-wide, so it is fetched on open rather than
            riding along with every page load. */}
        {view === "admin" && <AdminView setError={setError} />}
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
