"use client";

import type { Conversation, DocumentKind, FavoriteKind } from "@workspace/api-client";
import { BarChart3, ChevronDown, ChevronRight, CircleDot, Columns2, LogOut, Menu, Pencil, Plus, Share2, Trash2, Users, X } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "./api";
import { ApiHealthBanner, useApiHealth } from "./api-health-banner";
import { useSession } from "./auth/session-provider";
import {
  PaneToggle,
  collapseLabel,
  persistSectionCollapsed,
  readSectionCollapsed,
  useCollapsiblePane,
} from "./collapsible-pane";
import { CommandPalette } from "./command-palette";
import { CreateMenu } from "./create-menu";
import { WorkspaceSettingsMenu } from "./settings-menu";
import { SystemStatus } from "./system-status";
import { useWorkspace } from "./use-workspace";
import { InboxView, asCall } from "./views/inbox";
import { AdminView } from "./views/admin";
import { DatasetsView } from "./views/datasets";
import { AgentsView } from "./views/agents";
import { SpacesView } from "./views/spaces";
import { spaceNameOf } from "./views/space-threads";
import { AppsView } from "./views/apps";
import { BoardView } from "./views/board";
import { ChatView } from "./views/chat";
import { ChatSplit, readStoredSizes } from "./views/chat-split";
import type { DashboardPinning } from "./views/dashboard-pin-bar";
import { DashboardEditor } from "./views/dashboard-editor";
import { DashboardsView } from "./views/dashboards";
import { DataView } from "./views/data";
import { DocumentsView } from "./views/documents";
import { GraphView } from "./views/graph";
import { IntegrationsView } from "./views/integrations";
import { McpView } from "./views/mcp";
import { MemoryView } from "./views/memory";
import {
  CHORDS_KEY,
  CHORD_WINDOW_MS,
  chordEligible,
  chordTarget,
  parseChordsEnabled,
  serializeChordsEnabled,
} from "./views/chords";
import {
  LAYOUTS_KEY_PREFIX,
  layoutNames,
  parseStoredLayouts,
  removeLayout,
  serializeStoredLayouts,
  upsertLayout,
  type SavedLayouts,
} from "./views/layouts";
import { SPLIT_SIZES_KEY, serializeStoredSizes } from "./views/split-sizes";
import { THREAD_OPEN_KEY, parseThreadOpen, type ThreadOpen } from "./views/thread-open";
import type { PaletteToggle } from "./views/palette";
import { FAVORITE_VIEW, FavoriteStar, FavoritesNav, useFavorites } from "./views/favorites";
import {
  DEFAULT_GROUP_VIEW,
  NAV_GROUPS,
  RAIL_GROUPS,
  groupForView,
  type CreateAction,
  type GroupId,
} from "./views/navigation";
import { PoliciesView } from "./views/policies";
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
import { SNOOZE_KEY, parseSnoozes, snoozedIds } from "./views/snooze";
import { SourcesView } from "./views/sources";
import { ThemeToggle } from "./theme-toggle";
import { WorkflowsView } from "./views/workflows";
import { CronsView } from "./views/crons";
import { WorkspaceSwitcher, useWorkspaceSelection } from "./workspace-selection";

/**
 * The saved-layouts store for one workspace, and the two palette preferences.
 * Guarded for private-mode / server render like every other `grain.*` read:
 * a throwing or absent store is simply the default, never a crash. The
 * decodes live in their pure modules; these wrappers only add the guard.
 */
function readStoredLayouts(key: string): SavedLayouts {
  if (typeof window === "undefined") return {};
  try {
    return parseStoredLayouts(window.localStorage.getItem(key));
  } catch {
    return {};
  }
}

function readThreadOpen(): ThreadOpen {
  if (typeof window === "undefined") return "primary";
  try {
    return parseThreadOpen(window.localStorage.getItem(THREAD_OPEN_KEY));
  } catch {
    return "primary";
  }
}

function readChordsEnabled(): boolean {
  if (typeof window === "undefined") return true;
  try {
    return parseChordsEnabled(window.localStorage.getItem(CHORDS_KEY));
  } catch {
    return true;
  }
}

/** Persist one `grain.*` key, shrugging in private mode like every other write. */
function writeStored(key: string, value: string) {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Private mode: the choice just does not survive a reload.
  }
}

export function Workspace() {
  const {
    bootstrap,
    conversations,
    activeConversation,
    messages,
    sources,
    spaces,
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
    applyLayout,
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
    notice,
    setNotice,
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
    focusedSource,
    setFocusedSource,
    focusedMemory,
    setFocusedMemory,
    focusedEntity,
    setFocusedEntity,
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
    boards,
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
    removeConversation,
    decideAgentCall,
    steerActiveRun,
    setApprovalMode,
    cancelActiveRun,
    regenerate,
    editMessage,
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

  // The one health loop the shell runs: the red banner and the system-status
  // dot both read it, so they cannot disagree for a poll cycle.
  const health = useApiHealth(api, loadWorkspace);

  // The ONE favorites list, instantiated here and passed down: the sidebar
  // block, the thread row's star, the Documents header and the dashboard
  // catalog all read and write this same state, so no two of them can drift.
  const favorites = useFavorites(setError);

  /**
   * Where a favorite lands, per kind. Conversations, documents, projects and
   * dashboards have focus plumbing, so their rows open the thing itself.
   * Boards, workflows, agents and schedules do not yet — for those, landing on
   * the view that lists them is the honest v1 rather than a focus we would
   * have to fake.
   */
  function openFavorite(kind: FavoriteKind, targetId: string) {
    setSidebarOpen(false);
    if (kind === "conversation") {
      // The thread-open preference governs here exactly as it does the
      // palette's Enter: "split" means beside the primary, not instead of it.
      // A refused pane (already the primary, split full) falls back to the
      // plain open — a favorite click must always land on the thread.
      if (threadOpen === "split" && openInNewPane(targetId)) return;
      return void selectConversation(targetId);
    }
    if (kind === "document") {
      setView("documents");
      return void openDocument(targetId);
    }
    if (kind === "project") {
      setView("projects");
      return void openProject(targetId);
    }
    if (kind === "dashboard") {
      setView("dashboards");
      // Launching IS pinning — the catalog's own precedent — so an unpinned
      // favorite pins on open rather than focusing a tile the grid never
      // renders.
      if (!pinnedIds.has(targetId)) void pinDashboard(targetId);
      return setFocusedDashboard(targetId);
    }
    setView(FAVORITE_VIEW[kind]);
  }

  // The open thread's own row, which is where its approval mode lives. Read
  // from the rail's list rather than held separately, so the picker and the
  // bypass indicator cannot disagree about which mode is in force.
  const activeThread = conversations.find((item) => item.id === activeConversation);
  const activeTitle = activeThread?.title || "New conversation";

  // The one pin bundle, shared by the primary chat and every extra pane — a
  // dashboard made in a side pane gets the same finish-the-job bar.
  const pinning: DashboardPinning = {
    dashboards,
    pinnedIds,
    pin: pinDashboard,
    // "Show me" lands on the Dashboards view focused on the one the turn
    // made — the same landing the sidebar's pin rows use.
    open: (id) => {
      setView("dashboards");
      setSidebarOpen(false);
      setFocusedDashboard(id);
    },
    // Appended rather than assigned: a half-written message in the composer
    // is not this button's to discard.
    askForChart: (seed) => setDraft((current) => (current ? `${current}\n${seed}` : seed)),
  };

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

  // Saved layouts: named snapshots of the whole split, per workspace — the
  // switcher REMOUNTS this shell, so the key is read once per mount and never
  // goes stale. Write-through rather than an effect: only a save or delete
  // changes the store, and each already knows the next value whole.
  const { currentId: workspaceId } = useWorkspaceSelection();
  const layoutsKey = LAYOUTS_KEY_PREFIX + workspaceId;
  const [layouts, setLayouts] = useState<SavedLayouts>(() => readStoredLayouts(layoutsKey));
  // Bumped after an apply writes ratios on the split's behalf, so ChatSplit
  // re-reads them even when the column count did not change. The sizes ride
  // beside it as a prop too: the storage write is best-effort (private mode
  // swallows it), and the apply must not depend on it having landed.
  const [splitResetKey, setSplitResetKey] = useState(0);
  const [appliedSizes, setAppliedSizes] = useState<number[] | null>(null);

  function persistLayouts(next: SavedLayouts) {
    setLayouts(next);
    writeStored(layoutsKey, serializeStoredLayouts(next));
  }

  /** Capture the split as it stands: primary thread, extra panes, and the
   *  ratios the current column count last committed. */
  function saveLayoutAs(name: string) {
    // As RENDERED, not as stored: ghost panes (deleted conversations the
    // prune has not caught) are filtered out of what ChatSplit draws, so a
    // snapshot of the raw list would embalm dead panes and read sizes for a
    // column count the user never saw.
    const known = new Set(conversations.map((item) => item.id));
    const live =
      known.size > 0
        ? extraPanes.filter((pane) => known.has(pane.conversationId))
        : extraPanes;
    const next = upsertLayout(layouts, name, {
      primaryConversationId: activeConversation,
      panes: live,
      sizes: readStoredSizes(1 + live.length),
    });
    if (next === layouts) {
      // upsertLayout refused the name (blank, or one the parser would drop
      // on the next load) — saying "saved" here would be a vanishing act.
      setNotice({ text: "That name can’t be used for a layout", at: Date.now() });
      return;
    }
    persistLayouts(next);
    setNotice({ text: `Saved layout “${name.trim()}”`, at: Date.now() });
  }

  /** Recall a saved layout: ratios into the store first (so the split's forced
   *  re-read finds them), then panes and primary in one commit. */
  function applySavedLayout(name: string) {
    const layout = layouts[name];
    if (!layout) return;
    if (layout.sizes.length === layout.panes.length + 1 && typeof window !== "undefined") {
      try {
        writeStored(
          SPLIT_SIZES_KEY,
          serializeStoredSizes(
            window.localStorage.getItem(SPLIT_SIZES_KEY),
            layout.panes.length + 1,
            layout.sizes,
          ),
        );
      } catch {
        // Private mode: the ratios just do not land; the panes still do.
      }
    }
    applyLayout(layout.panes, layout.primaryConversationId);
    setAppliedSizes(layout.sizes.length > 0 ? layout.sizes : null);
    setSplitResetKey((value) => value + 1);
  }

  function deleteSavedLayout(name: string) {
    persistLayouts(removeLayout(layouts, name));
    setNotice({ text: `Deleted layout “${name}”`, at: Date.now() });
  }

  // The two palette preferences: where a thread opens, and whether the
  // G-chords listen at all. Read once (any earlier choice), written on flip.
  const [threadOpen, setThreadOpen] = useState<ThreadOpen>(() => readThreadOpen());
  const [chordsEnabled, setChordsEnabled] = useState<boolean>(() => readChordsEnabled());

  function togglePreference(toggle: PaletteToggle) {
    if (toggle === "thread-open") {
      const next: ThreadOpen = threadOpen === "split" ? "primary" : "split";
      setThreadOpen(next);
      writeStored(THREAD_OPEN_KEY, next);
      setNotice({
        text: next === "split" ? "Threads now open in a split" : "Threads now open in place",
        at: Date.now(),
      });
    } else {
      const next = !chordsEnabled;
      setChordsEnabled(next);
      writeStored(CHORDS_KEY, serializeChordsEnabled(next));
      setNotice({
        text: next ? "Keyboard shortcuts on" : "Keyboard shortcuts off",
        at: Date.now(),
      });
    }
  }

  // ⌘\ / Ctrl+\ — the split's own key. With extra panes open it cycles the
  // split's focus (primary → pane 1 → … → primary); with none it says where a
  // split comes from rather than silently doing nothing. Window-level and live
  // in typing contexts too, like ⌘K: the modifier is what keeps it out of the
  // text's way, and the preventDefault keeps it from the browser.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!(event.metaKey || event.ctrlKey) || event.key !== "\\") return;
      event.preventDefault();
      if (extraPanes.length === 0) {
        setNotice({
          text: "Open a thread in a split from its rail row, or ⌘⏎ in the palette",
          at: Date.now(),
        });
        return;
      }
      const order: (string | null)[] = [null, ...extraPanes.map((pane) => pane.id)];
      // A focused id no longer in the order lands at -1, so the cycle starts
      // over from the primary rather than throwing.
      const next = order[(order.indexOf(focusedPane) + 1) % order.length];
      focusPane(next);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [extraPanes, focusedPane, focusPane, setNotice]);

  // G-chords: G then a letter jumps to a destination (G C chat, G I inbox,
  // G L library, G A automations, G D dashboards). Bare letters only, and
  // never from a field — anything that edits text keeps its keys, so the
  // chord cannot yank the view mid-sentence. The armed G lives in a local
  // variable rather than state: it re-renders nothing and dies with the
  // listener.
  useEffect(() => {
    let armedAt = 0;
    const onKey = (event: KeyboardEvent) => {
      if (!chordEligible(chordsEnabled, event)) return;
      const now = Date.now();
      if (armedAt && now - armedAt <= CHORD_WINDOW_MS) {
        // A second G re-arms rather than lapsing, so "g g c" still lands.
        armedAt = event.key.toLowerCase() === "g" ? now : 0;
        const target = chordTarget(event.key);
        if (target) {
          event.preventDefault();
          // A completed chord is spoken for: without this, "G A" would also
          // reach the Inbox's bubble-phase A/D triage listener and approve
          // whatever row it had focused. Capture phase (below) is what puts
          // this handler ahead of that one regardless of mount order.
          event.stopPropagation();
          setView(target);
          setSidebarOpen(false);
        }
        return;
      }
      if (event.key.toLowerCase() === "g") armedAt = now;
    };
    window.addEventListener("keydown", onKey, { capture: true });
    return () => window.removeEventListener("keydown", onKey, { capture: true });
  }, [setView, setSidebarOpen, chordsEnabled]);

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
      {/* The star rides only the OPEN thread, beside rename, for the same
          noise argument. The list is the shell's one favorites state, so the
          sidebar block above updates in the same render. */}
      {activeConversation === conversation.id && (
        <FavoriteStar
          kind="conversation"
          targetId={conversation.id}
          label={conversation.title}
          favorites={favorites}
          className="thread-favorite"
          size={13}
        />
      )}
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
    // One merged destination, so it counts everything it shows: boards and
    // one-column lists live in the same listing now.
    boards: boards.length,
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

  /**
   * Which sidebar shelves are folded away, keyed "<groupId>.<sectionId>" and
   * persisted per section like the panes. One state object rather than a hook
   * per section, because the section count changes with the active group and
   * hooks may not come and go. Read once in the initialiser — same
   * no-hydration argument as `useCollapsiblePane` — and written through on
   * each toggle. The unlabelled "main" block has no heading to press, so it
   * is never collapsible and never read here.
   */
  const [collapsedSections, setCollapsedSections] = useState<Record<string, boolean>>(
    () => {
      const initial: Record<string, boolean> = {};
      for (const group of NAV_GROUPS) {
        for (const section of group.sections) {
          if (!section.label) continue;
          initial[`${group.id}.${section.id}`] = readSectionCollapsed(group.id, section.id);
        }
      }
      return initial;
    },
  );
  function toggleSection(groupId: GroupId, sectionId: string) {
    const key = `${groupId}.${sectionId}`;
    const next = !collapsedSections[key];
    persistSectionCollapsed(groupId, sectionId, next);
    setCollapsedSections((current) => ({ ...current, [key]: next }));
  }

  // Landing on a view whose shelf is folded unfolds it (and persists the
  // unfold): the sidebar must always show an aria-current row for where you
  // are, and a route that arrives sideways — the palette, a G-chord, an
  // upload landing on Sources — must not strand you in an unmarked map.
  useEffect(() => {
    const group = groupForView(view);
    const section = group.sections.find(
      (candidate) =>
        candidate.label && candidate.items.some((item) => item.view === view),
    );
    if (!section) return;
    const key = `${group.id}.${section.id}`;
    setCollapsedSections((current) => {
      if (!current[key]) return current;
      persistSectionCollapsed(group.id, section.id, false);
      return { ...current, [key]: false };
    });
  }, [view]);

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
  // Snoozes deliberately do NOT subtract from it: the badge counts facts
  // (requests waiting on a human), and sleeping through one does not make it
  // stop waiting. Only the nag surfaces — the strip below and the Inbox's
  // approvals tab — honour a snooze.
  const inboxBadge = inbox
    ? inbox.approvals.length + inbox.budget_holds.length
    : pendingApprovals.length;

  // The Inbox's "Later" schedule, re-read whenever the feed refreshes, the
  // user moves between views, or the Inbox reports a change — same-tab state,
  // so no storage event fires and these are the sync points. The callback
  // matters when the Inbox and this strip are on screen together: a snooze
  // written there must leave the doorbell in the same render cycle.
  const [snoozedApprovals, setSnoozedApprovals] = useState<Set<string>>(new Set());
  const rereadSnoozes = () =>
    setSnoozedApprovals(
      snoozedIds(parseSnoozes(window.localStorage.getItem(SNOOZE_KEY)), new Date()),
    );
  useEffect(() => {
    rereadSnoozes();
  }, [inbox, view]);
  const waitingRows = inbox
    ? inbox.approvals.filter((row) => !snoozedApprovals.has(row.id))
    : [];

  /** One icon-only destination button, shared by the rail and the tab bar. */
  const renderGroupButton = (group: (typeof RAIL_GROUPS)[number]) => {
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
        aria-label={badge > 0 ? `${group.label} ${badge}` : group.label}
        title={group.label}
        onClick={() => openGroup(group.id)}
      >
        <Icon size={19} />
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
        openThreadInSplit={openInNewPane}
        create={create}
        searchTranscripts={(q) => api.searchConversations(q)}
        // The Phase-6 rows: saved layouts by name, and the two preference
        // toggles. All of it exists only through here — zero resident chrome
        // is the point — so an unwired palette would make the whole feature
        // dead code.
        extras={{
          layoutNames: layoutNames(layouts),
          threadOpen,
          chordsEnabled,
        }}
        applyLayout={applySavedLayout}
        saveLayout={saveLayoutAs}
        deleteLayout={deleteSavedLayout}
        togglePreference={togglePreference}
      />

      {/* Band 1: the icon rail — four doors, always visible on desktop. The
          places you work and nothing else; creating and configuring live in
          the top-right, and everything deeper is summoned per destination in
          the contextual sidebar beside this. */}
      <nav className="icon-rail" aria-label="Workspace">
        {RAIL_GROUPS.map((group) => renderGroupButton(group))}
      </nav>

      {/* Band 2: the contextual sidebar — what the chosen door opens onto.
          Collapsible to nothing (the icon rail keeps you oriented); on mobile
          it is the drawer, holding everything but the destinations — those
          ride the bottom tab bar there. */}
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
            {activeGroup.sections.map((section) => {
              const collapsed = Boolean(
                collapsedSections[`${activeGroup.id}.${section.id}`],
              );
              const rows = section.items.map((item) => {
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
              });
              // The unlabelled block is never collapsible: it holds the
              // destination's primary views, and it has no heading to press.
              if (!section.label) {
                return (
                  <div className="section-block" key={section.id}>
                    {rows}
                  </div>
                );
              }
              const Chevron = collapsed ? ChevronRight : ChevronDown;
              return (
                <div className="section-block" key={section.id}>
                  {/* The heading is its own collapse toggle. The accessible
                      name carries the outcome and keeps the visible label in
                      it ("Hide the Data section"), same contract as the pane
                      toggles; aria-expanded carries the state. Deliberately
                      NOT named "<label>" bare — the entry buttons below are,
                      and two controls answering to one name is ambiguity the
                      specs would trip over before a screen reader did. */}
                  <button
                    type="button"
                    className="section-heading"
                    aria-expanded={!collapsed}
                    aria-label={collapseLabel(`${section.label} section`, collapsed)}
                    onClick={() => toggleSection(activeGroup.id, section.id)}
                  >
                    {section.label}
                    <Chevron size={12} aria-hidden="true" />
                  </button>
                  {!collapsed && rows}
                </div>
              );
            })}
          </nav>
        )}

        {/* Waiting-on-you: the top of the Inbox, rendered where the eye lands
            first. A run that parked overnight greets the user before they open
            anything; each row is decidable in place, and the strip caps at two
            because it is a doorbell, not the door — the Inbox is one click up.
            Snoozed rows stay out of it: this is a nag surface, and "Later"
            means later here too. The rail badge above still counts them. */}
        {waitingRows.length > 0 && (
          <div className="waiting-strip" role="group" aria-label="Waiting on you">
            <span className="waiting-strip-title">Waiting on you</span>
            {waitingRows.slice(0, 2).map((row) => (
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
            {waitingRows.length > 2 && (
              <button className="waiting-strip-more" onClick={() => openGroup("inbox")}>
                {waitingRows.length - 2} more in Inbox
              </button>
            )}
          </div>
        )}

        {/* In every destination's sidebar for the pinned-dashboards reason
            below: a favorite is a thing *this user* wanted in reach wherever
            they are. Above the pins, and a separate nav — the pinned block is
            dashboards-only and stays exactly as it is. Renders nothing while
            the list is empty. */}
        <FavoritesNav favorites={favorites} onOpen={openFavorite} />

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
        // A backdrop, not a control: the drawer's X is the accessible close —
        // giving this the same name put two "Close menu" buttons on screen at
        // once, a strict-mode ambiguity for tests and a duplicate stop for
        // AT. Pointer-only dismiss is what a scrim is; keyboard users have
        // the X and Escape-adjacent focus order inside the drawer.
        <div
          className="sidebar-scrim"
          aria-hidden="true"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      <main className="main-panel">
        <ApiHealthBanner api={api} health={health} />
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
            {/* The screen and provider pills, folded into one popover: the
                facts they spelt out are posture, not news, and belong behind a
                dot that only colours when something is actually wrong. */}
            <SystemStatus bootstrap={bootstrap} apiDown={health.down} />
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

        {/* The neutral toast — a presentation change (a list graduating to a
            board), never a failure. Its own class and role="status", because
            the red toast is role="alert" and pinned absent by specs that
            expect nothing to have gone wrong. */}
        {notice && (
          <div className="notice-toast" role="status">
            <span>{notice.text}</span>
            <button onClick={() => setNotice(null)} aria-label="Dismiss notice">
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
            // Bumped when a recalled layout writes ratios for an UNCHANGED
            // column count — the one case the count-keyed re-read cannot see.
            // The sizes ride as a prop so a private-mode storage failure
            // cannot silently drop them.
            resetKey={splitResetKey}
            forcedSizes={appliedSizes}
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
            pinning={pinning}
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
                steer={steerActiveRun}
                regenerate={regenerate}
                editMessage={editMessage}
                // The signed-in member, so a shared thread offers the pencil
                // only on their own prompts.
                viewerId={session?.user_id}
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
                pinning={pinning}
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
            graph={graph}
            spaces={spaces}
            openEntity={(id) => {
              setView("graph");
              setFocusedEntity(id);
            }}
            focused={focusedSource}
            setFocused={setFocusedSource}
          />
        )}

        {view === "memory" && (
          <MemoryView
            memories={memories}
            forgetMemory={forgetMemory}
            conversations={conversations}
            spaces={spaces}
            graph={graph}
            // Already lands on the chat view; the id is enough.
            openConversation={(id) => void selectConversation(id)}
            openEntity={(id) => {
              setView("graph");
              setFocusedEntity(id);
            }}
            focused={focusedMemory}
            setFocused={setFocusedMemory}
          />
        )}

        {view === "graph" && (
          <GraphView
            graph={graph}
            rebuild={rebuildKnowledgeGraph}
            openChunk={openChunk}
            sources={sources}
            openSource={(id) => {
              setView("sources");
              setFocusedSource(id);
            }}
            openMemory={(id) => {
              setView("memory");
              setFocusedMemory(id);
            }}
            focused={focusedEntity}
            setFocused={setFocusedEntity}
          />
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
            removeDashboard={removeDashboard}
            focused={focusedDashboard}
            setFocused={setFocusedDashboard}
            favorites={favorites}
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
            pendingEdits={pendingEdits}
            decidePendingEdit={decidePendingEdit}
            favorites={favorites}
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
            boards={boards}
            createBoard={createBoard}
            addCard={addBoardCard}
            moveCard={moveBoardCard}
            removeCard={removeBoardCard}
            removeBoard={removeBoard}
            columnOps={boardColumnOps}
            todoOps={todoOps}
            favorites={favorites}
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
            setEntry={setProjectEntry}
            removeProject={removeProject}
            favorites={favorites}
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
            conversations={conversations}
            sources={sources}
            setError={setError}
            refreshSpaces={refreshSecondary}
            onSelectConversation={selectConversation}
            onNewThread={(spaceId) => void newConversation(spaceId)}
          />
        )}
        {view === "agents" && (
          <AgentsView
            setError={setError}
            // "Save & try" creates its thread through the raw client, which
            // the shell's conversations list never hears about — so the list
            // is re-read alongside the select, or the rail shows no row, the
            // title falls back to "New conversation", and the approval chip
            // reads the ask_writes default instead of the thread's own mode.
            openConversation={(id) => {
              void refreshConversations().catch(() => undefined);
              void selectConversation(id);
            }}
            favorites={favorites}
          />
        )}

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
            favorites={favorites}
          />
        )}

        {/* Self-contained like WorkflowsView: a cron's schedule and last-fired
            state are nobody's business until they open this, so the list is
            fetched here rather than at page load. */}
        {view === "crons" && <CronsView setError={setError} favorites={favorites} />}

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

        {view === "activity" && (
          <InboxView
            feed={inbox}
            refreshFeed={() => void refreshSecondary().catch(() => undefined)}
            events={auditEvents}
            decide={decideAgentCall}
            activeRun={activeRun}
            openConversation={(id) => void selectConversation(id)}
            onSnoozesChanged={rereadSnoozes}
          />
        )}

        {/* Member-readable by design — the rules ledger and the org posture
            both fetch for any caller, so this mounts with no role gate. */}
        {view === "policies" && <PoliciesView setError={setError} />}

        {/* Owner-only and workspace-wide, so it is fetched on open rather than
            riding along with every page load. */}
        {view === "admin" && <AdminView setError={setError} />}
      </main>

      {/* The four doors again, as a thumb-height bottom bar — mobile only.
          The icon rail hides under the drawer breakpoint, and a drawer is a
          place destinations go to be forgotten; the bar keeps them one tap
          away while the drawer keeps what a drawer is for (the switcher,
          sections, threads, identity). Same buttons as the rail — badge
          included — so the two surfaces cannot drift apart. */}
      <nav className="mobile-tab-bar" aria-label="Destinations">
        {RAIL_GROUPS.map((group) => renderGroupButton(group))}
      </nav>

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
