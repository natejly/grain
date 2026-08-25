"use client";

import type { Conversation, DocumentKind } from "@workspace/api-client";
import { BarChart3, CircleDot, Columns2, LogOut, Menu, MessageSquareText, Pencil, Plus, Share2, ShieldAlert, Trash2, Users, X } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "./api";
import { ApiHealthBanner } from "./api-health-banner";
import { useSession } from "./auth/session-provider";
import { PaneToggle, useCollapsiblePane } from "./collapsible-pane";
import { CommandPalette } from "./command-palette";
import { CreateMenu } from "./create-menu";
import { WorkspaceSettingsMenu } from "./settings-menu";
import { useWorkspace } from "./use-workspace";
import { actionableApprovals } from "./views/approval-format";
import { InboxView, asCall } from "./views/inbox";
import { AdminView } from "./views/admin";
import { DatasetsView } from "./views/datasets";
import { AgentsView } from "./views/agents";
import { SpacesView } from "./views/spaces";
import { spaceNameOf } from "./views/space-threads";
import { AppsView } from "./views/apps";
import { BoardView } from "./views/board";
import { ChatView } from "./views/chat";
import { ChatSplit } from "./views/chat-split";
import { CommentsPanel, type CommentSubject } from "./views/comments";
import { DashboardEditor } from "./views/dashboard-editor";
import { DashboardsView } from "./views/dashboards";
import { DataView } from "./views/data";
import { DocumentsView } from "./views/documents";
import { GraphView } from "./views/graph";
import { IntegrationsView } from "./views/integrations";
import { McpView } from "./views/mcp";
import { WebhooksView } from "./views/webhooks";
import { MemoryView } from "./views/memory";
import {
  DEFAULT_GROUP_VIEW,
  RAIL_GROUPS,
  groupForView,
  type CreateAction,
  type GroupId,
} from "./views/navigation";
import { ProjectsView } from "./views/projects";
import { SandboxToolsView } from "./views/sandbox-tools";
import {
  PAGE_TITLES,
  formatRelative,
  groupThreads,
  shareControl,
  type View,
} from "./views/shared";
import { SkillsView } from "./views/skills";
import { SourcesView } from "./views/sources";
import { TodosView } from "./views/todos";
import { ThemeToggle } from "./theme-toggle";
import { WorkflowsView } from "./views/workflows";
import { CronsView } from "./views/crons";
import { MonitorsView } from "./views/monitors";
import { WorkspaceSwitcher } from "./workspace-selection";

export function Workspace() {
  const {
    bootstrap,
    conversations,
    activeConversation,
    messages,
    sources,
    spaces,
    spaceTemplates,
    agentCalls,
    auditEvents,
    inbox,
    createDatasetFromSource,
    createDatasetVersionFromSource,
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
    pendingEdits,
    dbConnections,
    projects,
    activeProject,
    view,
    setView,
    extraPanes,
    focusedPane,
    openInNewPane,
    closePane,
    focusPane,
    refreshConversations,
    patchConversation,
    shareConversation,
    renameConversation,
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
    focusedDashboard,
    setFocusedDashboard,
    loadWorkspace,
    refreshOffScreenWork,
    refreshSecondary,
    refreshPendingEdits,
    reloadOpenDocument,
    reloadOpenProject,
    refreshDashboards,
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
    setProjectEntry,
    removeProject,
    addMcpServer,
    refreshMcpServer,
    setMcpServerEnabled,
    setMcpToolEnabled,
    removeMcpServer,
    addSandboxTool,
    setSandboxToolEnabled,
    removeSandboxTool,
    selectConversation,
    newConversation,
    forkThread,
    undoRun,
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
    duplicateDashboard,
    removeDashboard,
    publishGeneratedApp,
    rollbackGeneratedApp,
    connectIntegration,
    connectGarminAccount,
    disconnectIntegration,
    syncIntegration,
    loadComments,
    addComment,
    removeComment,
    loadMembers,
    resolveMention,
    resolveAlert,
    resolveAnomaly,
    assignApproval,
  } = useWorkspace();
  // Always present: this component only renders inside the authenticated gate.
  const { session, signOut } = useSession();

  // The open thread's own row, which is where its approval mode lives. Read
  // from the rail's list rather than held separately, so the picker and the
  // bypass indicator cannot disagree about which mode is in force.
  const activeThread = conversations.find((item) => item.id === activeConversation);
  const activeTitle = activeThread?.title || "New conversation";

  // The rail's two audiences. A thread is shared with the whole workspace or it
  // is the caller's own — the server never returns another member's personal
  // thread, so `shared` alone tells the groups apart.
  const { personal: personalThreads, shared: sharedThreads } = groupThreads(conversations);

  /**
   * One rail row. The share/unshare toggle rides only the OPEN thread and only
   * when the caller may change its visibility (its creator or a workspace
   * owner) — a member seeing it on every row could not act on most of them, and
   * a control that 403s on click is worse than no control.
   */
  // Which rail row is an input right now, and what it says. Shell state (not
  // row state) so exactly one rename can be open at a time.
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");

  // What the comments drawer is about, or null when closed. One subject state
  // for all three surfaces (thread, document, dashboard) because one drawer
  // serves them all — the subject pair is the server's own shape.
  const [commentSubject, setCommentSubject] = useState<CommentSubject | null>(null);

  // ⌘K / Ctrl+K, from anywhere — inputs included, which is why this handler
  // exists at the window and preventDefaults: the browser wants the shortcut
  // for its own search bar.
  const [paletteOpen, setPaletteOpen] = useState(false);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen((value) => !value);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  async function submitRename(conversation: Conversation) {
    const title = renameDraft.trim();
    setRenamingId(null);
    if (!title || title === conversation.title) return;
    await renameConversation(conversation.id, title);
  }

  const renderThread = (conversation: Conversation) => {
    const share = shareControl(conversation, activeConversation);
    if (renamingId === conversation.id) {
      return (
        <div key={conversation.id} className="thread active">
          <input
            className="thread-rename-input"
            value={renameDraft}
            aria-label={`Rename ${conversation.title}`}
            autoFocus
            onChange={(event) => setRenameDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                void submitRename(conversation);
              }
              if (event.key === "Escape") setRenamingId(null);
            }}
            onBlur={() => void submitRename(conversation)}
          />
        </div>
      );
    }
    return (
    <div
      key={conversation.id}
      className={activeConversation === conversation.id ? "thread active" : "thread"}
    >
      <button
        className="thread-open"
        onClick={() => selectConversation(conversation.id)}
      >
        <span>{conversation.title}</span>
        {spaceNameOf(conversation, spaces) && (
          <span className="thread-space-chip">
            {spaceNameOf(conversation, spaces)}
          </span>
        )}
        <time>{formatRelative(conversation.updated_at)}</time>
      </button>
      {/* Rename rides only the OPEN thread, like share: an affordance on every
          row is sidebar noise. Subject threads are named by what they hang
          off, so they never offer it — the server refuses anyway. */}
      {activeConversation === conversation.id && !conversation.subject_id && (
        <button
          className="thread-rename"
          title="Rename thread"
          aria-label={`Rename ${conversation.title}`}
          onClick={(event) => {
            event.stopPropagation();
            setRenamingId(conversation.id);
            setRenameDraft(conversation.title);
          }}
        >
          <Pencil size={13} />
        </button>
      )}
      {share && (
        <button
          className={share.shared ? "thread-share shared" : "thread-share"}
          title={share.title}
          aria-label={share.ariaLabel}
          aria-pressed={share.pressed}
          onClick={(event) => {
            event.stopPropagation();
            void shareConversation(conversation.id, !conversation.shared);
          }}
        >
          {share.shared ? <Users size={13} /> : <Share2 size={13} />}
        </button>
      )}
      <button
        className="thread-split"
        title="Open in a new pane"
        aria-label={`Open ${conversation.title} in a new pane`}
        onClick={(event) => {
          event.stopPropagation();
          openInNewPane(conversation.id);
        }}
      >
        <Columns2 size={13} />
      </button>
      {/* Comments ride only the OPEN thread, like rename and share: the drawer
          is about the thread you are looking at, not any row you can reach. */}
      {activeConversation === conversation.id && (
        <button
          className="thread-comments"
          title="Comments on this thread"
          aria-label={`Comments on ${conversation.title}`}
          onClick={(event) => {
            event.stopPropagation();
            setCommentSubject({
              kind: "conversation",
              id: conversation.id,
              label: conversation.title,
            });
          }}
        >
          <MessageSquareText size={13} />
        </button>
      )}
      <button
        className="thread-delete"
        title="Delete chat"
        aria-label={`Delete ${conversation.title}`}
        onClick={(event) => void removeConversation(conversation, event)}
      >
        <Trash2 size={13} />
      </button>
    </div>
    );
  };

  // Per-view badge numbers, shown on the tab strip inside a group. A view with
  // no count (Chat, Graph, Inbox) is absent: Graph's size is a projection of
  // Sources, so counting it in the Knowledge badge would double-count what the
  // user actually put in. Deliberately NOT summed onto the rail: the rail's one
  // number is the Inbox's count of requests waiting on a human, and an
  // inventory figure beside it would make "3" mean stock on one row and
  // attention on the next.
  const viewCounts: Partial<Record<View, number>> = {
    sources: sources.length,
    memory: memories.length,
    documents: documents.length,
    projects: projects.length,
    // Each tab counts what that tab shows: a one-column board is on Lists,
    // not here, so counting it twice would make both numbers wrong.
    boards: kanbanBoards.length,
    todos: todoLists.length,
    datasets: datasets.length,
    dashboards: dashboards.length,
    apps: dashboardApps.length,
    data: dbConnections.length,
    mcp: mcpServers.length,
    "sandbox-tools": sandboxTools.length,
    integrations: integrations.filter((item) => item.account).length,
  };

  const activeGroup = groupForView(view);
  // A one-view group gets no section nav: a list with a single row is noise.
  const showSections = activeGroup.items.length > 1;

  // Where each group reopens. Tracking `view` rather than the click means an
  // action that navigates on its own — an upload landing on Sources, the OAuth
  // return landing on Integrations — is remembered too.
  /**
   * The rail, hidden on request. Separate from `sidebarOpen`, which is the
   * mobile off-canvas drawer and is deliberately not remembered: a drawer that
   * reopened itself on every load would cover the page you asked for.
   */
  const [railCollapsed, toggleRail] = useCollapsiblePane("rail");

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
    // so the composer *is* the creation step — same as an app's editor.
    if (action.id === "workflow") return setWorkflowRequested(true);
    // An app is named, bound to data and generated in one editor; opening
    // it *is* the creation step, and it lands on the Apps gallery when closed.
    return setEditing("new");
  }

  // The shell's ONE numeric badge: requests parked waiting on a human, on the
  // destination that answers them. Counted from the unbounded feed, not from
  // the fifty-row call window — the window is how this number used to read
  // zero over a real backlog. Until the feed's first read lands, the window
  // count stands in rather than showing a zero not yet known to be true.
  // An approval routed to a colleague is *their* wait, so it neither counts
  // here nor previews in the strip — it still lists in the Inbox, dimmed.
  const selfId = bootstrap?.identity.user_id ?? "";
  const waitingOnMe = inbox ? actionableApprovals(inbox.approvals, selfId) : [];
  const inboxBadge = inbox
    ? waitingOnMe.length +
      inbox.budget_holds.length +
      inbox.mentions.length +
      // Monitor alerts wait on a human too — '' -targeted, so they wait on
      // every member until one resolves them for the room.
      inbox.alerts.length +
      // Spend anomalies are the same broadcast shape: a workspace fact
      // waiting for anyone to acknowledge it. Left out of this sum they
      // would be invisible on the rail — the founding bug of this badge.
      inbox.anomalies.length
    : pendingApprovals.length;

  /** One rail/drawer destination button; `wide` is the mobile drawer's shape. */
  const renderGroupButton = (group: (typeof RAIL_GROUPS)[number], wide: boolean) => {
    const Icon = group.icon;
    const badge = group.id === "inbox" ? inboxBadge : 0;
    return (
      <button
        key={group.id}
        className={activeGroup.id === group.id ? "nav-item active" : "nav-item"}
        aria-current={activeGroup.id === group.id ? "page" : undefined}
        // The badge joins the accessible name (an icon-only button's aria-label
        // REPLACES its content for assistive tech, so a bare label would read
        // "Inbox" over three waiting requests).
        aria-label={
          wide ? undefined : badge > 0 ? `${group.label} ${badge}` : group.label
        }
        title={wide ? undefined : group.label}
        onClick={() => openGroup(group.id)}
      >
        <Icon size={wide ? 17 : 19} />
        {wide && group.label}
        {badge > 0 && <span className="approval-count">{badge}</span>}
      </button>
    );
  };

  return (
    <div className={railCollapsed ? "workspace-shell rail-collapsed" : "workspace-shell"}>
      <CommandPalette
        open={paletteOpen}
        close={() => setPaletteOpen(false)}
        conversations={conversations}
        openView={(next) => {
          setView(next);
          setSidebarOpen(false);
        }}
        openThread={(id) => void selectConversation(id)}
        create={create}
        searchTranscripts={(q) => api.searchConversations(q)}
      />

      {/* Band 1: the icon rail — four doors, always visible on desktop. The
          places you work and nothing else; creating and configuring live in
          the top-right, and everything deeper is summoned per destination in
          the contextual sidebar beside this. */}
      <nav className="icon-rail" aria-label="Workspace">
        {RAIL_GROUPS.map((group) => renderGroupButton(group, false))}
      </nav>

      {/* Band 2: the contextual sidebar — what the chosen door opens onto.
          Collapsible to nothing (the icon rail keeps you oriented); on mobile
          it is the drawer, and carries the destinations itself because the
          icon rail is hidden there. */}
      <aside id="workspace-rail" className={`sidebar ${sidebarOpen ? "sidebar-open" : ""}`}>
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

        {/* The icon rail is hidden under the drawer breakpoint, so the drawer
            carries the destinations itself — labelled, because a drawer has
            the width the rail deliberately does not. */}
        <nav className="primary-nav mobile-group-nav" aria-label="Destinations">
          {RAIL_GROUPS.map((group) => renderGroupButton(group, true))}
        </nav>

        {activeGroup.id === "chat" && (
          <button
            className="chrome-button new-thread-button"
            // Wrapped: newConversation takes an optional space id now, and a
            // MouseEvent must not arrive in that slot.
            onClick={() => void newConversation()}
          >
            <Plus size={16} />
            New thread
          </button>
        )}

        {/* This destination's own map: its views, under section headings that
            say what each shelf holds. What the seven-tab strip grew into. */}
        {showSections && (
          <nav className="section-nav" aria-label={`${activeGroup.label} views`}>
            {activeGroup.sections.map((section, index) => (
              <div className="section-block" key={section.label || index}>
                {section.label && (
                  <span className="section-heading">{section.label}</span>
                )}
                {section.items.map((item) => {
                  const Icon = item.icon;
                  const count = viewCounts[item.view];
                  return (
                    <button
                      key={item.view}
                      className={view === item.view ? "nav-item active" : "nav-item"}
                      aria-current={view === item.view ? "page" : undefined}
                      onClick={() => {
                        setView(item.view);
                        setSidebarOpen(false);
                      }}
                    >
                      <Icon size={15} />
                      {item.label}
                      {count !== undefined && <span className="nav-count">{count}</span>}
                    </button>
                  );
                })}
              </div>
            ))}
          </nav>
        )}

        {/* Waiting-on-you: the top of the Inbox, rendered where the eye lands
            first. A run that parked overnight greets the user before they open
            anything; each row is decidable in place, and the strip caps at two
            because it is a doorbell, not the door — the Inbox is one click up. */}
        {inbox && waitingOnMe.length > 0 && (
          <div className="waiting-strip" role="group" aria-label="Waiting on you">
            <span className="waiting-strip-title">Waiting on you</span>
            {waitingOnMe.slice(0, 2).map((row) => (
              <div key={row.id} className="waiting-strip-item">
                <button
                  className="waiting-strip-open"
                  onClick={() => {
                    if (row.conversation_id) {
                      void selectConversation(row.conversation_id);
                    } else {
                      openGroup("inbox");
                    }
                  }}
                >
                  <strong>{row.name}</strong>
                  <span>
                    {row.conversation_title || row.origin} ·{" "}
                    {formatRelative(row.created_at)}
                  </span>
                </button>
                <button
                  className="icon-button waiting-approve"
                  title={`Approve ${row.name}`}
                  aria-label={`Approve ${row.name}`}
                  onClick={() =>
                    void decideAgentCall(asCall(row), "approved", false)
                      .then(refreshSecondary)
                      .catch(() => undefined)
                  }
                >
                  ✓
                </button>
                <button
                  className="icon-button waiting-deny"
                  title={`Deny ${row.name}`}
                  aria-label={`Deny ${row.name}`}
                  onClick={() =>
                    void decideAgentCall(asCall(row), "denied", false)
                      .then(refreshSecondary)
                      .catch(() => undefined)
                  }
                >
                  ✕
                </button>
              </div>
            ))}
            {waitingOnMe.length > 2 && (
              <button className="waiting-strip-more" onClick={() => openGroup("inbox")}>
                {waitingOnMe.length - 2} more in Inbox
              </button>
            )}
          </div>
        )}

        {/* In every destination's sidebar, deliberately: a pin is not a place
            the product has, it is a chart *this user* wanted where they could
            see it — and "where they could see it" means wherever they are.
            Its own nav so a screen reader is told which is which. */}
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

        {/* Personal threads (only their creator sees them) and the workspace's
            shared threads are two different audiences, so they get two labelled
            groups rather than one flat list that hides which teammates can read
            what. The server already returns only the caller's own personal
            threads plus every shared thread in the workspace, so `shared` alone
            splits them. */}
        {activeGroup.id === "chat" && (
          <>
            <div className="thread-heading">
              <span>Recent threads</span>
            </div>
            <div className="thread-list">
              {conversations.length === 0 ? (
                <p className="empty-threads">No conversations.</p>
              ) : (
                <>
                  {personalThreads.length > 0 && (
                    <>
                      <div className="thread-group">Personal</div>
                      {personalThreads.map(renderThread)}
                    </>
                  )}
                  {sharedThreads.length > 0 && (
                    <>
                      <div className="thread-group">Shared</div>
                      {sharedThreads.map(renderThread)}
                    </>
                  )}
                </>
              )}
            </div>
          </>
        )}

        {/* Chat's thread list is the flexing element; every other destination
            needs this spacer so the identity block still sits at the bottom. */}
        {activeGroup.id !== "chat" && <div className="sidebar-spacer" />}

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
          {/* The rail's toggle lives out here rather than in the rail, because
              the rail collapses to nothing: a control inside it would go with
              it, and the only way back would be to clear localStorage. Hidden
              below the breakpoint where the rail is a drawer with its own
              open/close pair. */}
          <PaneToggle
            subject="sidebar"
            collapsed={railCollapsed}
            toggle={toggleRail}
            controls="workspace-rail"
          />
          <div className="page-context">
            {showSections && <span>{activeGroup.label}</span>}
            <strong>{view === "chat" ? activeTitle : PAGE_TITLES[view]}</strong>
          </div>
          <div className="topbar-actions">
            <CreateMenu create={create} />
            <WorkspaceSettingsMenu activeGroup={activeGroup.id} open={openGroup} />
            <ThemeToggle />
            {/* The prompt-injection screen's posture, shown only when it is on:
                a status indicator, not a control. "enforce" is the mode that
                actually escalates a flagged turn, so it reads as active; shadow
                reads as watching. The proxy URL never reaches the client. */}
            {bootstrap?.screen.enabled && (
              <div
                className={`screen-pill ${bootstrap.screen.mode}`}
                title={`Prompt-injection screen: ${bootstrap.screen.mode} mode, ${bootstrap.screen.backend} backend`}
              >
                <ShieldAlert size={13} aria-hidden="true" />
                Screen: {bootstrap.screen.mode}
              </div>
            )}
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

        {/* The tab strip is gone: a destination's siblings live in its
            contextual sidebar, under section headings, where seven of them
            read as a shelf rather than a lineup. */}

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
          // The chat surface is a split: the shell's own ChatView (pane 0,
          // passed through unchanged so the single-pane user sees zero change)
          // plus any extra concurrent panes. With no extra panes ChatSplit
          // renders the primary alone with no wrapper — the no-regression path.
          <ChatSplit
            panes={extraPanes}
            conversations={conversations}
            bootstrap={bootstrap}
            sources={sources}
            apps={dashboardApps}
            openCitation={openCitation}
            closePane={closePane}
            focusedPane={focusedPane}
            focusPane={focusPane}
            onSettled={refreshConversations}
            onApprovalChanged={patchConversation}
            primary={
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
                flaggedRuns={flaggedRuns}
                sharedThread={activeThread?.shared}
                submitPrompt={submitPrompt}
                cancelActiveRun={cancelActiveRun}
                regenerate={regenerate}
                decideAgentCall={decideAgentCall}
                openCitation={openCitation}
                attach={{
                  upload: uploadFiles,
                  uploading,
                  createDataset: createDatasetFromSource,
                }}
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
                skills={{
                  attached: attachedSkill,
                  argValues: skillArgs,
                  attach: attachSkill,
                  detach: detachSkill,
                  setArg: setSkillArg,
                }}
                fork={forkThread}
                undo={undoRun}
              />
            }
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

        {view === "datasets" && (
          <DatasetsView
            datasets={datasets}
            sources={sources}
            createDataset={createDatasetFromSource}
            createVersion={createDatasetVersionFromSource}
            // The cross-link that closes the connect-data → chart-it trek:
            // dashboards are written by the agent, so "Chart this" hands the
            // composer the sentence and goes where the agent answers.
            chartThis={(dataset) => {
              setDraft(`Build a chart from the “${dataset.name}” dataset: `);
              setView("chat");
              setSidebarOpen(false);
            }}
            setError={setError}
          />
        )}

        {view === "apps" && (
          <AppsView
            apps={dashboardApps}
            openEditor={setEditing}
            publish={publishGeneratedApp}
            rollback={rollbackGeneratedApp}
          />
        )}

        {view === "dashboards" && (
          <DashboardsView
            apps={dashboardApps}
            askForChart={() => {
              setDraft("Build a chart from my data: ");
              setView("chat");
              setSidebarOpen(false);
            }}
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
            duplicateDashboard={duplicateDashboard}
            removeDashboard={removeDashboard}
            focused={focusedDashboard}
            setFocused={setFocusedDashboard}
            openComments={(dashboard) =>
              setCommentSubject({
                kind: "dashboard",
                id: dashboard.id,
                label: dashboard.name,
              })
            }
            chat={{
              agentId: bootstrap?.default_agent_id,
              sources,
              openCitation,
              // A revised spec changes what the tile draws, so the list is
              // re-read and the open tile re-run — otherwise the panel says it
              // changed the chart while the chart still shows the old one.
              reloadDashboards: async () => {
                await refreshDashboards();
                if (focusedDashboard) runDashboard(focusedDashboard, true);
              },
              unrestricted: bootstrap?.unrestricted_agent,
            }}
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
            openComments={(document) =>
              setCommentSubject({
                kind: "document",
                id: document.id,
                label: document.title,
              })
            }
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
              unrestricted: bootstrap?.unrestricted_agent,
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
            setEntry={setProjectEntry}
            removeProject={removeProject}
            chat={{
              agentId: bootstrap?.default_agent_id,
              sources,
              apps: dashboardApps,
              openCitation,
              // The panel's own run finished: re-read the project it was about,
              // so an approved write reaches the tree, the editor and the
              // preview. Nothing else in the shell is refreshed from here.
              reloadProject: reloadOpenProject,
              unrestricted: bootstrap?.unrestricted_agent,
            }}
          />
        )}

        {/* Self-contained like WorkflowsView: the agent list is fetched when
            somebody opens the editor, not at page load. */}
        {view === "spaces" && (
          <SpacesView
            spaces={spaces}
            spaceTemplates={spaceTemplates}
            conversations={conversations}
            sources={sources}
            setError={setError}
            refreshSpaces={refreshSecondary}
            onSelectConversation={selectConversation}
            onNewThread={(spaceId) => void newConversation(spaceId)}
          />
        )}
        {view === "agents" && <AgentsView setError={setError} />}

        {/* Self-contained like AgentsView: the skill list is nobody's business
            until they open this or type "/" in the composer. */}
        {view === "skills" && <SkillsView setError={setError} />}

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

        {/* Self-contained like WorkflowsView: a cron's schedule and last-fired
            state are nobody's business until they open this, so the list is
            fetched here rather than at page load. */}
        {view === "crons" && <CronsView setError={setError} />}

        {/* Self-contained like CronsView, but the dataset picker reads the
            shell's datasets list — the same rows the Datasets page shows. */}
        {view === "monitors" && (
          <MonitorsView setError={setError} datasets={datasets} />
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

        {view === "sandbox-tools" && (
          <SandboxToolsView
            tools={sandboxTools}
            addTool={addSandboxTool}
            setToolEnabled={setSandboxToolEnabled}
            removeTool={removeSandboxTool}
          />
        )}

        {/* Self-contained like CronsView: tokens and endpoints are owner
            configuration, fetched when the settings page opens and never at
            page load. */}
        {view === "webhooks" && <WebhooksView setError={setError} />}

        {view === "activity" && (
          <InboxView
            feed={inbox}
            refreshFeed={() => void refreshSecondary().catch(() => undefined)}
            events={auditEvents}
            decide={decideAgentCall}
            activeRun={activeRun}
            openConversation={(id) => void selectConversation(id)}
            resolveMention={resolveMention}
            resolveAlert={resolveAlert}
            openMonitors={() => setView("monitors")}
            resolveAnomaly={resolveAnomaly}
            // Where this reader can act on spend: owners get the usage panel
            // on Admin; a member cannot see that page, so they land on the
            // Agents view the anomaly is about.
            openSpending={() =>
              setView(
                bootstrap?.identity.role === "owner" ? "admin" : "agents",
              )
            }
            identityId={selfId}
            loadMembers={loadMembers}
            assignApproval={assignApproval}
            openMention={(mention) => {
              // The deep link in precedence order: the thread the comment sits
              // on, else the document, else the dashboard (revealed the way the
              // rail reveals a pin).
              if (mention.conversation_id) {
                void selectConversation(mention.conversation_id);
                return;
              }
              if (mention.document_id) {
                void openDocument(mention.document_id);
                setView("documents");
                return;
              }
              if (mention.dashboard_id) {
                setFocusedDashboard(mention.dashboard_id);
                setView("dashboards");
              }
            }}
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

      {commentSubject && (
        <CommentsPanel
          subject={commentSubject}
          close={() => setCommentSubject(null)}
          ops={{
            load: loadComments,
            add: addComment,
            remove: removeComment,
            loadMembers,
          }}
          currentUserId={session?.user_id ?? ""}
          isOwner={bootstrap?.identity.role === "owner"}
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
