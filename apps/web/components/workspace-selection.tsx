"use client";

import type { WorkspaceMembership } from "@workspace/api-client";
import { Check, ChevronsUpDown } from "lucide-react";
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

const STORAGE_KEY = "jasmine.workspace-id";

type WorkspaceSelectionValue = {
  /** Every workspace the user belongs to; empty when the list could not load. */
  workspaces: WorkspaceMembership[];
  currentId: string;
  select: (workspaceId: string) => void;
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

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      let listed: WorkspaceMembership[] = [];
      try {
        listed = await api.listWorkspaces();
      } catch {
        // The switcher is chrome, not a gate. With no list there is nothing to
        // switch between, and the session's own workspace opens as it always did.
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
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const select = useCallback(
    (workspaceId: string) => {
      if (workspaceId === currentId) return;
      api.setWorkspaceId(workspaceId);
      persist(workspaceId);
      setCurrentId(workspaceId);
    },
    [currentId],
  );

  const value = useMemo(
    () => ({ workspaces: workspaces ?? [], currentId, select }),
    [workspaces, currentId, select],
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
export function WorkspaceSwitcher() {
  const { workspaces, currentId, select } = useWorkspaceSelection();
  const [open, setOpen] = useState(false);

  // Nothing loaded means nothing to switch between; the identity chip at the
  // bottom of the sidebar already says who is signed in.
  if (workspaces.length === 0) return null;

  const current = workspaces.find((item) => item.id === currentId) ?? workspaces[0];

  return (
    <div
      className="workspace-switcher"
      onKeyDown={(event) => {
        if (event.key === "Escape") setOpen(false);
      }}
    >
      <button
        className="workspace-switcher-trigger"
        aria-expanded={open}
        aria-controls="workspace-switcher-list"
        // The visible text is the workspace name, which alone would not say
        // what the control does; this names the purpose and the current value.
        aria-label={`Switch workspace (current: ${current.name})`}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="workspace-switcher-mark">
          {current.name.slice(0, 1).toUpperCase()}
        </span>
        <span className="workspace-switcher-name">{current.name}</span>
        <ChevronsUpDown size={13} />
      </button>

      {open && (
        <>
          <button
            className="workspace-switcher-scrim"
            aria-label="Close workspace list"
            onClick={() => setOpen(false)}
          />
          {/* A disclosure of real buttons rather than role="menu": a menu owes
              its users arrow-key navigation and a roving tabindex, and plain
              buttons in a named group get Tab and Enter for nothing. */}
          <div
            className="workspace-switcher-menu"
            id="workspace-switcher-list"
            role="group"
            aria-label="Your workspaces"
          >
            {workspaces.map((item) => (
              <button
                key={item.id}
                className={
                  item.id === current.id
                    ? "workspace-option active"
                    : "workspace-option"
                }
                aria-current={item.id === current.id ? "true" : undefined}
                onClick={() => {
                  setOpen(false);
                  select(item.id);
                }}
              >
                <span className="workspace-option-name">{item.name}</span>
                <span className="workspace-option-role">{item.role}</span>
                {item.id === current.id && <Check size={13} />}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
