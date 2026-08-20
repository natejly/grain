"use client";

import type {
  AdminActivity,
  AdminAuditPage,
  AdminBudget,
  AdminInvite,
  AdminMcpServer,
  AdminMember,
  AdminObservability,
  AdminSandboxSession,
  AdminStorage,
  AdminUsage,
} from "@workspace/api-client";
import { ApiError } from "@workspace/api-client";
import { RefreshCw, ShieldCheck, Square } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { useWorkspaceSelection } from "../workspace-selection";
import { ObservabilityPanel } from "./admin-observability";
import { UsagePanel } from "./admin-usage";
import { BudgetPanel } from "./budget";
import { InvitesPanel, MembersPanel } from "./members";
import { OrganizationPanel } from "./organization";
import { describeError, formatBytes, formatRelative } from "./shared";

/**
 * Workspace administration: who is in it, what it is doing, what it holds.
 *
 * Like the sandbox panel, this owns its own data rather than joining the
 * workspace hook — every route behind it is owner-only and aggregates the whole
 * workspace, so it is fetched when someone opens this view and never on a page
 * load that was only going to show chat.
 *
 * A member who is not an owner gets 403 from all of them. That is not an error
 * to shout about; it is the answer, so it renders as a sentence rather than a
 * red toast.
 */
export type AdminViewProps = {
  setError: (message: string) => void;
};

const AUDIT_PAGE = 25;
/** Long enough to cover a billing month; the API allows 1–365. */
const DEFAULT_USAGE_DAYS = 30;
/** A day is the window an owner opens this on; the API allows 1–720 hours. */
const DEFAULT_OBS_HOURS = 24;

type AdminData = {
  members: AdminMember[];
  invites: AdminInvite[];
  activity: AdminActivity;
  sandboxes: AdminSandboxSession[];
  mcpServers: AdminMcpServer[];
  storage: AdminStorage;
  usage: AdminUsage;
  budget: AdminBudget;
  observability: AdminObservability;
};

/** A status → count map as a row of pills, in a stable order. */
function CountRow({ counts, empty }: { counts: Record<string, number>; empty: string }) {
  const entries = Object.entries(counts).sort(([a], [b]) => a.localeCompare(b));
  if (entries.length === 0) return <p className="admin-empty">{empty}</p>;
  return (
    <div className="admin-counts">
      {entries.map(([status, count]) => (
        <span key={status} className="admin-count">
          <strong>{count}</strong>
          {status}
        </span>
      ))}
    </div>
  );
}

function Panel({
  title,
  count,
  children,
}: {
  title: string;
  count?: number;
  children: React.ReactNode;
}) {
  return (
    <section className="admin-panel">
      <div className="panel-title">
        <div>
          <strong>{title}</strong>
        </div>
        {count !== undefined && <span className="panel-count">{count}</span>}
      </div>
      {children}
    </section>
  );
}

export function AdminView({ setError }: AdminViewProps) {
  const { workspaces, currentId } = useWorkspaceSelection();
  const [data, setData] = useState<AdminData | null>(null);
  const [audit, setAudit] = useState<AdminAuditPage | null>(null);
  const [offset, setOffset] = useState(0);
  const [forbidden, setForbidden] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState("");
  const [usageDays, setUsageDays] = useState(DEFAULT_USAGE_DAYS);
  const [obsHours, setObsHours] = useState(DEFAULT_OBS_HOURS);

  // Both windows are part of `load` rather than fetches of their own: changing
  // either re-runs the same aggregates through the same 403 handling, and one
  // path that can fail is easier to keep right than two. The old panels stay on
  // screen while it re-fetches, because `setData` only fires on success.
  const load = useCallback(async () => {
    try {
      const [
        members,
        invites,
        activity,
        sandboxes,
        mcpServers,
        storage,
        usage,
        budget,
        observability,
      ] =
        await Promise.all([
          api.listAdminMembers(),
          api.listAdminInvites(),
          api.getAdminActivity(),
          api.listAdminSandboxSessions(),
          api.listAdminMcpServers(),
          api.getAdminStorage(),
          api.getAdminUsage(usageDays),
          api.getAdminBudget(),
          api.getAdminObservability(obsHours),
        ]);
      setData({
        members,
        invites,
        activity,
        sandboxes,
        mcpServers,
        storage,
        usage,
        budget,
        observability,
      });
      setForbidden(false);
    } catch (caught) {
      // 403 is the API answering, not failing: this member is not an owner.
      if (caught instanceof ApiError && caught.forbidden) setForbidden(true);
      else setError(describeError(caught, "Could not load the admin panels"));
    } finally {
      setLoaded(true);
    }
  }, [setError, usageDays, obsHours]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const page = await api.listAdminAuditEvents(AUDIT_PAGE, offset);
        if (!cancelled) setAudit(page);
      } catch (caught) {
        // The owner check already has a home above; a paging failure here would
        // otherwise replace the whole screen with an error it did not cause.
        if (!cancelled && !(caught instanceof ApiError && caught.forbidden)) {
          setError(describeError(caught, "Could not load the audit log"));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [offset, setError]);

  // The usage ledger holds no text by design, so the costliest runs take their
  // names from the Runs panel's own fetch rather than from a second request for
  // prompts the accounting tables deliberately do not store.
  const runPrompts = useMemo(
    () =>
      new Map(
        (data?.activity.recent_runs ?? []).map((run) => [run.id, run.prompt_preview]),
      ),
    [data],
  );

  async function kill(session: AdminSandboxSession) {
    if (!window.confirm("Stop this sandbox? Anything running inside it is lost.")) {
      return;
    }
    setBusy(session.id);
    try {
      const killed = await api.killAdminSandboxSession(session.id);
      setData((current) =>
        current
          ? {
              ...current,
              sandboxes: current.sandboxes.map((row) =>
                row.id === killed.id ? killed : row,
              ),
            }
          : current,
      );
    } catch (caught) {
      setError(describeError(caught, "Could not stop that sandbox"));
    } finally {
      setBusy("");
    }
  }

  if (forbidden) {
    return (
      <section className="content-page">
        <div className="empty-state">
          <ShieldCheck size={22} />
          <p>
            Admin is owner-only. Ask an owner of this workspace if you need a
            member removed, a run investigated, or a sandbox stopped.
          </p>
        </div>
      </section>
    );
  }

  if (!data) {
    return (
      <section className="content-page">
        <div className="empty-state">
          <ShieldCheck size={22} />
          <p>{loaded ? "Nothing to administer yet." : "Loading the workspace…"}</p>
        </div>
      </section>
    );
  }

  const {
    members,
    invites,
    activity,
    sandboxes,
    mcpServers,
    storage,
    usage,
    budget,
    observability,
  } = data;
  const live = sandboxes.filter((row) => ["running", "paused"].includes(row.status));

  return (
    <section className="content-page admin-page">
      <div className="page-heading">
        <div>
          <h1>Admin</h1>
        </div>
        <button className="ghost-button" onClick={() => void load()}>
          <RefreshCw size={14} /> Refresh
        </button>
      </div>

      <div className="admin-grid">
        {/* First, and full width: it is the only panel here that is about money,
            and the one whose numbers cost something to ignore. */}
        <UsagePanel
          usage={usage}
          days={usageDays}
          onDaysChange={setUsageDays}
          prompts={runPrompts}
        />

        {/* Directly under the spend, because the two are one question asked
            twice: what did this cost, and what may it cost. A ceiling read on
            a different screen from the figure it bounds gets raised by the
            wrong amount. */}
        <BudgetPanel
          budget={budget}
          onSaved={(saved) =>
            setData((current) => (current ? { ...current, budget: saved } : current))
          }
        />

        {/* Full width under the money panels: it is about the same run history,
            asked as a question of health rather than cost, and its bars and
            tables want the room for the same reason the spend breakdown does. */}
        <ObservabilityPanel
          observability={observability}
          hours={obsHours}
          onHoursChange={setObsHours}
        />

        {/* Above the roster, because it is the answer to the question the roster
            raises next: these are the people you can promote, and this is the
            authority you cannot promote them past. It owns its own fetch — an
            org read is not owner-gated, so folding it into the load above would
            make its failure look like the owner-only refusal, which it is not. */}
        <OrganizationPanel setError={setError} />

        {/* The roster and the queue feeding it, side by side: an owner opens
            this view to answer "who is here and who is arriving", and reading
            the two on separate screens is how somebody ends up re-inviting a
            person who accepted an hour ago. */}
        <MembersPanel
          members={members}
          onChange={(next) =>
            setData((current) => (current ? { ...current, members: next } : current))
          }
          setError={setError}
        />

        <InvitesPanel
          invites={invites}
          onChange={(next) =>
            setData((current) => (current ? { ...current, invites: next } : current))
          }
          setError={setError}
        />

        {/* The memberships the switcher already loaded — the API deliberately
            has no second endpoint over the same rows. */}
        <Panel title="Workspaces" count={workspaces.length}>
          <table className="admin-table">
            <thead>
              <tr>
                <th scope="col">Workspace</th>
                <th scope="col">Your role</th>
              </tr>
            </thead>
            <tbody>
              {workspaces.map((workspace) => (
                <tr key={workspace.id}>
                  <td>
                    <strong>{workspace.name}</strong>
                    {workspace.id === currentId && <span>open now</span>}
                  </td>
                  <td>
                    <span className="admin-tag">{workspace.role}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>

        <Panel title="Runs" count={activity.recent_runs.length}>
          <CountRow counts={activity.run_status_counts} empty="No runs yet." />
          <ul className="admin-list">
            {activity.recent_runs.map((run) => (
              <li key={run.id}>
                <div>
                  <strong>{run.prompt_preview || "(no prompt)"}</strong>
                  <span>
                    {run.status} · {formatRelative(run.created_at)}
                  </span>
                  {run.error && <small className="admin-error">{run.error}</small>}
                </div>
              </li>
            ))}
          </ul>
        </Panel>

        {/* The read-only "Awaiting approval" panel is gone on purpose: the
            Inbox lists the same parked calls for every member AND decides
            them. A second copy an owner could look at but not act on taught
            people to check two places and trust neither. Tool-call counts stay
            — they are operations telemetry, not a queue. */}
        <Panel title="Tool calls" count={0}>
          <CountRow
            counts={activity.tool_call_status_counts}
            empty="No tool calls yet."
          />
          <p className="admin-empty">
            Parked approvals are decided in the Inbox, not here.
          </p>
        </Panel>

        <Panel title="Sandbox sessions" count={live.length}>
          {sandboxes.length === 0 ? (
            <p className="admin-empty">No sandbox has ever been opened here.</p>
          ) : (
            <table className="admin-table">
              <thead>
                <tr>
                  <th scope="col">Session</th>
                  <th scope="col">Network</th>
                  <th scope="col">Used</th>
                  <th scope="col">
                    <span className="visually-hidden">Stop</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {sandboxes.map((session) => (
                  <tr key={session.id}>
                    <td>
                      <strong>{session.label || session.provider}</strong>
                      <span>
                        {session.status} · {session.exec_count} runs
                      </span>
                    </td>
                    <td>
                      <span className="admin-tag">{session.network_policy}</span>
                    </td>
                    <td>{formatRelative(session.last_used_at)}</td>
                    <td>
                      {["running", "paused"].includes(session.status) && (
                        <button
                          className="ghost-button"
                          disabled={busy === session.id}
                          aria-label={`Stop sandbox ${session.label || session.id}`}
                          onClick={() => void kill(session)}
                        >
                          <Square size={12} /> Stop
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>

        <Panel title="MCP servers" count={mcpServers.length}>
          {mcpServers.length === 0 ? (
            <p className="admin-empty">No MCP server is configured.</p>
          ) : (
            <table className="admin-table">
              <thead>
                <tr>
                  <th scope="col">Server</th>
                  <th scope="col">Status</th>
                  <th scope="col">Tools</th>
                </tr>
              </thead>
              <tbody>
                {mcpServers.map((server) => (
                  <tr key={server.id}>
                    <td>
                      <strong>{server.name}</strong>
                      <span>
                        {server.transport}
                        {server.has_secrets ? " · secrets stored" : ""}
                      </span>
                    </td>
                    <td>
                      <span className="admin-tag">
                        {server.enabled ? server.status : "disabled"}
                      </span>
                      {server.last_error && (
                        <small className="admin-error">{server.last_error}</small>
                      )}
                    </td>
                    <td>{server.tool_count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>

        <Panel title="Storage and indexing">
          <div className="admin-stats">
            <div>
              <strong>{storage.source_count}</strong>
              <span>sources</span>
            </div>
            <div>
              <strong>{formatBytes(storage.source_bytes)}</strong>
              <span>stored</span>
            </div>
            <div>
              <strong>{storage.chunk_count}</strong>
              <span>chunks</span>
            </div>
            <div>
              <strong>{storage.memory_item_count}</strong>
              <span>memories</span>
            </div>
            <div>
              <strong>{storage.graph_entity_count}</strong>
              <span>entities</span>
            </div>
            <div>
              <strong>{storage.graph_edge_count}</strong>
              <span>edges</span>
            </div>
          </div>
          <CountRow counts={storage.sources_by_status} empty="Nothing indexed yet." />
        </Panel>

        <Panel title="Audit log" count={audit?.total}>
          {!audit || audit.entries.length === 0 ? (
            <p className="admin-empty">Nothing recorded yet.</p>
          ) : (
            <>
              <ul className="admin-list">
                {audit.entries.map((entry) => (
                  <li key={entry.id}>
                    <div>
                      <strong>{entry.action.replaceAll(".", " ")}</strong>
                      <span>
                        {entry.actor_name || entry.actor_email || "system"} ·{" "}
                        {entry.resource_type} · {formatRelative(entry.created_at)}
                      </span>
                    </div>
                  </li>
                ))}
              </ul>
              <div className="admin-pager">
                <button
                  className="ghost-button"
                  disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - AUDIT_PAGE))}
                >
                  Newer
                </button>
                <span>
                  {offset + 1}–{offset + audit.entries.length} of {audit.total}
                </span>
                <button
                  className="ghost-button"
                  disabled={!audit.has_more}
                  onClick={() => setOffset(offset + AUDIT_PAGE)}
                >
                  Older
                </button>
              </div>
            </>
          )}
        </Panel>
      </div>
    </section>
  );
}
