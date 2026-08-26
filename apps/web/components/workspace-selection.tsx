"use client";

import type { WorkspaceMembership } from "@workspace/api-client";
import { Check, ChevronsUpDown, Plus, RefreshCw } from "lucide-react";
import {
  Fragment,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { api } from "./api";
import { AuthSplash } from "./auth/auth-screen";
import { DisclosureMenu } from "./disclosure-menu";

/**
 * Which of the user's workspaces the app is about, and the control that changes
 * it.
 *
 * A user with three workspaces used to be stuck in whichever one the API picked
 * by default, with the other two — their sources, memories and graph — simply
 * unreachable. The API already accepted `X-Workspace-Id` as a *selection* it
 * checks against memberships; what was missing was a way to discover the ids
 * and a place to say which one.
 *
 * This lives above the shell rather than inside it because switching is a
 * remount: `WorkspaceSelection` keys its subtree on the selected id, so every
 * piece of workspace state — sources, memories, graph, conversations, counts,
 * the open document, the active thread — is thrown away and refetched from
 * scratch. A partial refresh would leave whichever view nobody remembered still
 * showing the previous workspace's rows.
 */

const STORAGE_KEY = "grain.workspace-id";

type WorkspaceSelectionValue = {
  /** Every workspace the user belongs to; empty when the list could not load. */
  workspaces: WorkspaceMembership[];
  currentId: string;
  select: (workspaceId: string) => void;
  /**
   * Make a workspace and switch into it. Resolves once the shell is on its
   * way there, so the caller can close its menu; rejects with the API's
   * message so a duplicate-ish name or a lost connection is shown rather than
   * swallowed.
   *
   * The new row is pushed into `workspaces` here rather than refetched: the
   * `select` below remounts the whole shell on the new id, and a list that
   * has not caught up yet would render the switcher with the workspace the
   * user is standing in missing from it.
   */
  create: (name: string) => Promise<WorkspaceMembership>;
  /**
   * The list request failed. Distinguished from "belongs to nothing" because
   * the two demand opposite chrome: nothing-to-switch renders no switcher,
   * while a failed fetch renders a disabled one with a retry — a user with
   * three workspaces must not be silently locked into whichever one the
   * session defaulted to.
   */
  failed: boolean;
  retry: () => void;
};

const WorkspaceSelectionContext = createContext<WorkspaceSelectionValue | null>(null);

function readStored(): string {
  try {
    return window.localStorage.getItem(STORAGE_KEY) || "";
  } catch {
    // Private mode or a blocked origin: the session's own workspace still opens.
    return "";
  }
}

function persist(workspaceId: string): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, workspaceId);
  } catch {
    // The choice still applies to this tab; it just will not survive a reload.
  }
}

export function WorkspaceSelection({ children }: { children: React.ReactNode }) {
  // null means "not resolved yet", which is why it gates the children below: a
  // shell mounted before the selection is known would load the default
  // workspace and then immediately remount to load the stored one.
  const [workspaces, setWorkspaces] = useState<WorkspaceMembership[] | null>(null);
  const [currentId, setCurrentId] = useState("");
  const [failed, setFailed] = useState(false);
  // Bumped by retry(); the fetch effect re-runs on it.
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      let listed: WorkspaceMembership[] = [];
      let listFailed = false;
      try {
        listed = await api.listWorkspaces();
      } catch {
        // The switcher is chrome, not a gate: the session's own workspace
        // still opens. But the failure is remembered rather than swallowed, so
        // the switcher can say it is broken and offer a retry instead of
        // silently rendering nothing.
        listFailed = true;
      }
      if (cancelled) return;
      const stored = readStored();
      // A stored id the user no longer belongs to is not an error to show them:
      // sending it would 403 every request behind it, so a removed membership
      // would brick the app. It falls back to whatever this request resolved to.
      const chosen = listed.some((item) => item.id === stored)
        ? stored
        : listed.find((item) => item.is_current)?.id || "";
      if (chosen) api.setWorkspaceId(chosen);
      setCurrentId(chosen);
      setWorkspaces(listed);
      setFailed(listFailed);
    })();
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  const retry = useCallback(() => setAttempt((value) => value + 1), []);

  const select = useCallback(
    (workspaceId: string) => {
      if (workspaceId === currentId) return;
      api.setWorkspaceId(workspaceId);
      persist(workspaceId);
      setCurrentId(workspaceId);
    },
    [currentId],
  );

  const create = useCallback(async (name: string) => {
    const made = await api.createWorkspace(name);
    setWorkspaces((held) => [...(held ?? []), made]);
    // Same three steps as `select`, inlined rather than called: `select`
    // early-returns when the id already matches, and its `currentId`
    // dependency is a render behind the row we just added.
    api.setWorkspaceId(made.id);
    persist(made.id);
    setCurrentId(made.id);
    return made;
  }, []);

  const value = useMemo(
    () => ({ workspaces: workspaces ?? [], currentId, select, create, failed, retry }),
    [workspaces, currentId, select, create, failed, retry],
  );

  if (workspaces === null) return <AuthSplash message="Opening your workspace…" />;

  return (
    <WorkspaceSelectionContext.Provider value={value}>
      {/* The key is the whole refetch strategy: a new workspace id is a new
          element, so React unmounts the entire shell and mounts a fresh one,
          and no state from the old workspace can survive into it. A Fragment
          rather than a wrapper so the shell stays a direct child of <body>. */}
      <Fragment key={currentId}>{children}</Fragment>
    </WorkspaceSelectionContext.Provider>
  );
}

export function useWorkspaceSelection(): WorkspaceSelectionValue {
  const value = useContext(WorkspaceSelectionContext);
  if (!value) {
    throw new Error("useWorkspaceSelection must be used inside a WorkspaceSelection");
  }
  return value;
}

/**
 * The switcher itself, at the top of the sidebar because everything below it —
 * threads, sources, memory, the graph — is scoped to whatever it says.
 */
/**
 * The "start a new one" half of the switcher menu: a button that becomes an
 * input in place.
 *
 * In the menu rather than in settings because this is where a person already
 * goes when the question in their head is "which workspace?" — and "a new
 * one" is one of the answers to it.
 */
function CreateWorkspaceRow({ onCreated }: { onCreated: () => void }) {
  const { create } = useWorkspaceSelection();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  if (!open) {
    return (
      <button className="workspace-option create" onClick={() => setOpen(true)}>
        <Plus size={13} aria-hidden />
        <span className="workspace-option-name">New workspace</span>
      </button>
    );
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed || busy) return;
    setBusy(true);
    setError("");
    try {
      await create(trimmed);
      // Only now: closing the menu before the request lands would leave the
      // person with no idea whether it worked.
      onCreated();
    } catch (caught) {
      setBusy(false);
      setError(caught instanceof Error ? caught.message : "Could not create it");
    }
  }

  return (
    <form className="workspace-create-form" onSubmit={submit}>
      <input
        autoFocus
        className="workspace-create-input"
        placeholder="Workspace name"
        aria-label="New workspace name"
        value={name}
        maxLength={120}
        disabled={busy}
        onChange={(event) => setName(event.target.value)}
        // Escape backs out of the input without closing the whole menu, so a
        // mis-click costs nothing.
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.stopPropagation();
            setOpen(false);
            setName("");
          }
        }}
      />
      <button className="chrome-button" type="submit" disabled={busy || !name.trim()}>
        {busy ? "Creating…" : "Create"}
      </button>
      {error && (
        <span className="workspace-create-error" role="alert">
          {error}
        </span>
      )}
    </form>
  );
}

export function WorkspaceSwitcher() {
  const { workspaces, currentId, select, failed, retry } = useWorkspaceSelection();

  // The list request failed: say so where the switcher would be, with the one
  // action that helps. A silent null here reads as "you have no other
  // workspaces", which for a member of three is simply false.
  if (failed && workspaces.length === 0) {
    return (
      <div className="workspace-switcher-failed">
        <button
          className="chrome-button workspace-switcher-trigger"
          disabled
          aria-label="Workspaces unavailable"
        >
          <span className="workspace-switcher-mark">!</span>
          <span className="chrome-button-label">Workspaces unavailable</span>
        </button>
        <button className="ghost-button" onClick={retry}>
          <RefreshCw size={13} aria-hidden="true" />
          Retry
        </button>
      </div>
    );
  }

  // Nothing loaded means nothing to switch between; the identity chip at the
  // bottom of the sidebar already says who is signed in.
  if (workspaces.length === 0) return null;

  const current = workspaces.find((item) => item.id === currentId) ?? workspaces[0];

  return (
    <DisclosureMenu
      id="workspace-switcher-list"
      className="stretch"
      // The visible text is the workspace name, which alone would not say what
      // the control does; this names the purpose and the current value.
      triggerLabel={`Switch workspace (current: ${current.name})`}
      triggerClassName="chrome-button workspace-switcher-trigger"
      trigger={
        <>
          <span className="workspace-switcher-mark">
            {current.name.slice(0, 1).toUpperCase()}
          </span>
          <span className="chrome-button-label">{current.name}</span>
          <ChevronsUpDown size={13} />
        </>
      }
      menuLabel="Your workspaces"
      closeLabel="Close workspace list"
    >
      {(close) => (
        <>
          {workspaces.map((item) => (
            <button
              key={item.id}
              className={
                item.id === current.id ? "workspace-option active" : "workspace-option"
              }
              aria-current={item.id === current.id ? "true" : undefined}
              onClick={() => {
                close();
                select(item.id);
              }}
            >
              <span className="workspace-option-name">{item.name}</span>
              <span className="workspace-option-role">{item.role}</span>
              {item.id === current.id && <Check size={13} />}
            </button>
          ))}
          <CreateWorkspaceRow onCreated={close} />
        </>
      )}
    </DisclosureMenu>
  );
}
