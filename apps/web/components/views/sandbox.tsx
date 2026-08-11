"use client";

import {
  Cpu,
  Globe,
  LoaderCircle,
  Pause,
  Play,
  Plus,
  ShieldAlert,
  ShieldOff,
  Terminal,
  Trash2,
} from "lucide-react";
import type {
  SandboxExecution,
  SandboxExecutionKind,
  SandboxNetworkPolicy,
  SandboxSession,
} from "@workspace/api-client";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { ArtifactImages } from "../source-image";
import { describeError, formatRelative } from "./shared";

export type SandboxViewProps = {
  setError: (message: string) => void;
  /**
   * Set when the user picked "Sandbox" from the Create menu, which creates
   * rather than navigates. It is a request the panel *consumes* — it calls
   * `onStartHandled` before starting anything — because a plain boolean would
   * boot a second machine every time this view remounted while it was still up.
   */
  startRequested?: boolean;
  onStartHandled?: () => void;
};

/**
 * The panel owns its own data rather than joining the workspace hook, because a
 * sandbox is the one resource here that costs money by the minute: it must be
 * fetched when someone opens this view and not on every page load.
 */

/** Sessions that still name a live machine, and so can still be acted on. */
const LIVE: SandboxSession["status"][] = ["running", "paused"];

type PolicyNote = {
  icon: typeof Globe;
  tone: "open" | "allowlist" | "none";
  title: string;
  detail: string;
};

/**
 * What this machine can reach, said plainly.
 *
 * This is the most important thing on the page. ADR 0005 is explicit that the
 * real cost of server-side execution is exfiltration — a sandbox holding your
 * documents and holding a socket — so nobody should have to hunt for whether the
 * box they are about to paste a customer list into can talk to the internet.
 */
function policyNote(session: SandboxSession): PolicyNote {
  const policy: SandboxNetworkPolicy = session.network_policy;
  if (policy === "none") {
    return {
      icon: ShieldOff,
      tone: "none",
      title: "No network access",
      detail: "Nothing in this sandbox can reach the internet, and nothing can leave it.",
    };
  }
  if (policy === "allowlist") {
    return {
      icon: ShieldAlert,
      tone: "allowlist",
      title: `Egress limited to ${session.allow_hosts.length} host${
        session.allow_hosts.length === 1 ? "" : "s"
      }`,
      detail: session.allow_hosts.join(", ") || "Nothing is allowed out.",
    };
  }
  return {
    icon: Globe,
    tone: "open",
    title: "Full internet access",
    detail:
      "This sandbox can reach any host. Anything you paste into it — or any file " +
      "it reads — can be sent out by the code that runs here.",
  };
}

function statusLabel(session: SandboxSession): string {
  if (session.status === "running") return "Running";
  if (session.status === "paused") return "Paused";
  if (session.status === "killed") return "Stopped";
  return "Failed to start";
}

/**
 * One execution, rendered the way a terminal would: the command, then its
 * streams. stdout and stderr are visually distinct because "it printed nothing
 * and warned twice" and "it printed twice" are different outcomes, and a single
 * merged blob makes them look identical.
 */
function ExecutionBlock({ execution }: { execution: SandboxExecution }) {
  return (
    <article className="sandbox-exec">
      <header className="sandbox-exec-head">
        <span className="sandbox-exec-kind">
          {execution.kind === "command" ? "$" : ">>>"}
        </span>
        <pre className="sandbox-exec-source">{execution.source}</pre>
        <span className="sandbox-exec-meta">
          {execution.duration_ms}ms
          {execution.exit_code !== null && execution.exit_code !== 0
            ? ` · exit ${execution.exit_code}`
            : ""}
        </span>
      </header>
      {execution.stdout && <pre className="sandbox-stdout">{execution.stdout}</pre>}
      {execution.stderr && <pre className="sandbox-stderr">{execution.stderr}</pre>}
      {execution.error && <pre className="sandbox-exec-error">{execution.error}</pre>}
      <ArtifactImages artifacts={execution.artifacts} label="this execution" />
      {!execution.stdout &&
        !execution.stderr &&
        !execution.error &&
        execution.artifacts.length === 0 && (
          <p className="sandbox-exec-quiet">No output.</p>
        )}
    </article>
  );
}

export function SandboxView({
  setError,
  startRequested = false,
  onStartHandled,
}: SandboxViewProps) {
  const [sessions, setSessions] = useState<SandboxSession[]>([]);
  const [activeId, setActiveId] = useState("");
  // Artifacts ride on the execution row itself, so the console keeps no second
  // map keyed by execution id: the history endpoint returns the same figures
  // the run did, and a reload no longer empties the panel.
  const [history, setHistory] = useState<SandboxExecution[]>([]);
  const [source, setSource] = useState("");
  const [kind, setKind] = useState<SandboxExecutionKind>("code");
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);

  const active = sessions.find((session) => session.id === activeId) ?? null;

  const loadSessions = useCallback(async () => {
    try {
      const rows = await api.listSandboxSessions();
      setSessions(rows);
      setActiveId((current) => {
        if (current && rows.some((row) => row.id === current)) return current;
        return rows.find((row) => LIVE.includes(row.status))?.id ?? rows[0]?.id ?? "";
      });
    } catch (caught) {
      setError(describeError(caught, "Could not list sandbox sessions"));
    } finally {
      setLoaded(true);
    }
  }, [setError]);

  // Constant for this mount: did the panel open *because* Create asked for a
  // sandbox? If so the listing is left to the start effect below, so the POST
  // can never be overtaken by a GET that was issued before it.
  const bootWithStart = useRef(startRequested).current;

  useEffect(() => {
    if (bootWithStart) return;
    void loadSessions();
  }, [loadSessions, bootWithStart]);

  useEffect(() => {
    if (!activeId) {
      setHistory([]);
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const rows = await api.listSandboxExecutions(activeId);
        // The API answers newest first; the console reads oldest to newest.
        if (!cancelled) setHistory([...rows].reverse());
      } catch (caught) {
        if (!cancelled) setError(describeError(caught, "Could not load sandbox history"));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [activeId, setError]);

  function replace(updated: SandboxSession) {
    setSessions((rows) => rows.map((row) => (row.id === updated.id ? updated : row)));
  }

  async function startSession() {
    setBusy(true);
    try {
      const created = await api.createSandboxSession();
      setSessions((rows) =>
        rows.some((row) => row.id === created.id)
          ? rows.map((row) => (row.id === created.id ? created : row))
          : [created, ...rows],
      );
      setActiveId(created.id);
    } catch (caught) {
      setError(describeError(caught, "Could not start a sandbox"));
    } finally {
      setBusy(false);
    }
  }

  // "Sandbox" in the Create menu creates one. The request is consumed before
  // the machine is asked for, so a failed start is not retried forever, and
  // returning to this view later does not start another.
  useEffect(() => {
    if (!startRequested) return;
    onStartHandled?.();
    void loadSessions().then(startSession);
    // startSession is redefined every render; the request flag is the trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [startRequested]);

  async function run() {
    if (!active || !source.trim() || busy) return;
    setBusy(true);
    try {
      const result = await api.runInSandbox(active.id, source, kind);
      setHistory((rows) => [...rows, result.execution]);
      replace(result.session);
    } catch (caught) {
      setError(describeError(caught, "The sandbox could not run that"));
    } finally {
      setBusy(false);
    }
  }

  async function pause() {
    if (!active) return;
    try {
      replace(await api.pauseSandboxSession(active.id));
    } catch (caught) {
      setError(describeError(caught, "Could not pause the sandbox"));
    }
  }

  async function kill(session: SandboxSession) {
    try {
      replace(await api.killSandboxSession(session.id));
    } catch (caught) {
      setError(describeError(caught, "Could not stop the sandbox"));
    }
  }

  const note = active ? policyNote(active) : null;
  const canRun = Boolean(active && active.status === "running" && source.trim() && !busy);

  return (
    <div className="sandbox-layout">
      <aside className="sandbox-sidebar">
        <div className="sandbox-sidebar-head">
          <span>Sandboxes</span>
          <button
            className="icon-button"
            onClick={() => void startSession()}
            disabled={busy}
            aria-label="New sandbox"
          >
            <Plus size={16} />
          </button>
        </div>

        {sessions.length === 0 ? (
          <p className="sandbox-empty">
            {loaded
              ? "No sandboxes yet. Start one to run Python with real packages, or ask the assistant to."
              : "Loading…"}
          </p>
        ) : (
          <ul className="sandbox-items">
            {sessions.map((session) => (
              <li key={session.id}>
                <button
                  className={
                    session.id === activeId ? "sandbox-item active" : "sandbox-item"
                  }
                  onClick={() => setActiveId(session.id)}
                >
                  <Cpu size={14} />
                  <span className="sandbox-item-name">
                    {session.label || session.project_id || "Scratch sandbox"}
                  </span>
                  <span className={`sandbox-status ${session.status}`}>
                    {statusLabel(session)}
                  </span>
                </button>
                <div className="sandbox-item-meta">
                  <span>{session.provider}</span>
                  <span>{session.exec_count} runs</span>
                  <span>{formatRelative(session.last_used_at)}</span>
                </div>
                {session.error && <p className="sandbox-item-error">{session.error}</p>}
              </li>
            ))}
          </ul>
        )}
      </aside>

      {active && note ? (
        <section className="sandbox-main">
          <header className={`sandbox-policy ${note.tone}`}>
            <note.icon size={16} />
            <div>
              <strong>{note.title}</strong>
              <span>{note.detail}</span>
            </div>
            <div className="sandbox-policy-actions">
              {LIVE.includes(active.status) && (
                <>
                  <button
                    className="ghost-button"
                    onClick={() => void pause()}
                    disabled={active.status !== "running"}
                  >
                    <Pause size={14} /> Pause
                  </button>
                  <button className="ghost-button" onClick={() => void kill(active)}>
                    <Trash2 size={14} /> Stop
                  </button>
                </>
              )}
            </div>
          </header>

          <div className="sandbox-console">
            {history.length === 0 && !busy && (
              <p className="sandbox-exec-quiet">Nothing has run in this sandbox yet.</p>
            )}
            {history.map((execution) => (
              <ExecutionBlock key={execution.id} execution={execution} />
            ))}
            {busy && (
              <p className="sandbox-running">
                <LoaderCircle size={13} className="spin" /> Running…
              </p>
            )}
          </div>

          <div className="sandbox-editor">
            <div className="sandbox-editor-head">
              <label>
                <input
                  type="radio"
                  name="sandbox-kind"
                  checked={kind === "code"}
                  onChange={() => setKind("code")}
                />
                Python
              </label>
              <label>
                <input
                  type="radio"
                  name="sandbox-kind"
                  checked={kind === "command"}
                  onChange={() => setKind("command")}
                />
                Shell
              </label>
              <span className="field-hint">
                {kind === "code"
                  ? "Runs in the session's kernel — imports and variables survive between runs."
                  : "Runs a one-shot shell command in /home/user. This is the pip install path."}
              </span>
              <button
                className="primary-button"
                onClick={() => void run()}
                disabled={!canRun}
              >
                <Play size={14} /> Run
              </button>
            </div>
            <textarea
              className="sandbox-source"
              value={source}
              spellCheck={false}
              placeholder={
                kind === "code"
                  ? "import pandas as pd\nprint(pd.__version__)"
                  : "pip install seaborn"
              }
              onChange={(event) => setSource(event.target.value)}
              // Cmd/Ctrl+Enter is the notebook reflex; Enter alone must stay a
              // newline or multi-line code becomes unwritable.
              onKeyDown={(event) => {
                if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
                  event.preventDefault();
                  void run();
                }
              }}
            />
          </div>
        </section>
      ) : (
        <section className="sandbox-main empty">
          <div className="empty-state">
            <Terminal size={22} />
            <p>
              {loaded
                ? "Start a sandbox to run code on a real machine."
                : "Loading sandboxes…"}
            </p>
          </div>
        </section>
      )}
    </div>
  );
}
