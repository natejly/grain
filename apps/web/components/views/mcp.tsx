"use client";

import { Check, KeyRound, Plus, RefreshCw, Trash2, Unlink, X } from "lucide-react";
import type { McpAuthStatus, McpServer, McpServerInput } from "@workspace/api-client";
import { FormEvent, useCallback, useEffect, useState } from "react";
import { api } from "../api";

export type McpViewProps = {
  servers: McpServer[];
  addServer: (input: McpServerInput) => Promise<void>;
  refreshServer: (serverId: string) => Promise<void>;
  setServerEnabled: (serverId: string, enabled: boolean) => Promise<void>;
  setToolEnabled: (toolId: string, enabled: boolean) => Promise<void>;
  removeServer: (server: McpServer) => Promise<void>;
};

/**
 * Auth state for every remote server, owned here rather than by the workspace
 * hook.
 *
 * It is per user and it changes underneath the page — the browser leaves for
 * the authorization server and comes back — so it has to be re-read when this
 * view mounts. Folding it into the workspace bootstrap would make every page
 * load pay for it and would still be stale on return from consent.
 */
function useMcpAuth(servers: McpServer[]) {
  const [statuses, setStatuses] = useState<Record<string, McpAuthStatus>>({});
  const remoteIds = servers
    .filter((server) => server.transport === "http")
    .map((server) => server.id)
    .join(",");

  const reload = useCallback(async () => {
    const ids = remoteIds ? remoteIds.split(",") : [];
    const results = await Promise.all(
      ids.map(async (id) => {
        try {
          return await api.getMcpAuthStatus(id);
        } catch {
          // A status we cannot read must not blank the page; the server card
          // simply shows no auth pill.
          return null;
        }
      }),
    );
    const next: Record<string, McpAuthStatus> = {};
    for (const status of results) if (status) next[status.server_id] = status;
    setStatuses(next);
  }, [remoteIds]);

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(() => {
    // The callback lands on "/?mcp_connected=<id>". Strip it once read, so a
    // refresh or a shared link does not look like a fresh connection.
    const params = new URLSearchParams(window.location.search);
    if (!params.has("mcp_connected") && !params.has("mcp_error")) return;
    params.delete("mcp_connected");
    params.delete("mcp_error");
    const query = params.toString();
    window.history.replaceState({}, "", window.location.pathname + (query ? `?${query}` : ""));
    void reload();
  }, [reload]);

  return { statuses, reload };
}

/** Parse "KEY=value" lines into the secrets map the API expects. */
function parseSecrets(raw: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of raw.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const index = trimmed.indexOf("=");
    if (index <= 0) continue;
    out[trimmed.slice(0, index).trim()] = trimmed.slice(index + 1).trim();
  }
  return out;
}

function AddServerForm({
  onSubmit,
  onCancel,
}: {
  onSubmit: (input: McpServerInput) => Promise<void>;
  onCancel: () => void;
}) {
  const [transport, setTransport] = useState<"stdio" | "http">("stdio");
  const [name, setName] = useState("");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  const [url, setUrl] = useState("");
  const [secrets, setSecrets] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      await onSubmit({
        name: name.trim(),
        transport,
        command: command.trim(),
        // Shell-style splitting is deliberately not attempted; one arg per line.
        args: args.split("\n").map((item) => item.trim()).filter(Boolean),
        url: url.trim(),
        secrets: parseSecrets(secrets),
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="mcp-form" onSubmit={(event) => void submit(event)}>
      <div className="mcp-form-row">
        <label>
          Name
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="filesystem"
            required
          />
          <span className="field-hint">
            Tools appear to the model as mcp__{name || "name"}__&lt;tool&gt;
          </span>
        </label>
        <label>
          Transport
          <select
            value={transport}
            onChange={(event) => setTransport(event.target.value as "stdio" | "http")}
          >
            <option value="stdio">stdio (local subprocess)</option>
            <option value="http">streamable HTTP</option>
          </select>
        </label>
      </div>

      {transport === "stdio" ? (
        <div className="mcp-form-row">
          <label>
            Command
            <input
              value={command}
              onChange={(event) => setCommand(event.target.value)}
              placeholder="npx"
              required
            />
          </label>
          <label>
            Arguments (one per line)
            <textarea
              value={args}
              onChange={(event) => setArgs(event.target.value)}
              rows={3}
              placeholder={"-y\n@modelcontextprotocol/server-filesystem\n/path/to/dir"}
            />
          </label>
        </div>
      ) : (
        <label>
          URL
          <input
            value={url}
            onChange={(event) => setUrl(event.target.value)}
            placeholder="https://example.com/mcp"
            required
          />
        </label>
      )}

      <label>
        {transport === "stdio" ? "Environment" : "Headers"} (KEY=value per line)
        <textarea
          value={secrets}
          onChange={(event) => setSecrets(event.target.value)}
          rows={2}
          placeholder={
            transport === "stdio" ? "API_KEY=sk-…" : "Authorization=Bearer …"
          }
        />
        <span className="field-hint">Encrypted at rest and never read back.</span>
      </label>

      <div className="mcp-form-actions">
        <button type="button" className="ghost-button" onClick={onCancel}>
          Cancel
        </button>
        <button type="submit" className="primary-button" disabled={busy}>
          Add server
        </button>
      </div>
    </form>
  );
}

/**
 * The auth control for one remote server.
 *
 * Deliberately not a generic "settings" affordance: a server that needs OAuth
 * and has not got it is not broken, and the only useful thing to show is the
 * one button that fixes it.
 */
function AuthControls({
  server,
  status,
  onChanged,
  setError,
}: {
  server: McpServer;
  status: McpAuthStatus | undefined;
  onChanged: () => Promise<void>;
  setError: (message: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const needsAuth = server.status === "needs_auth";
  if (server.transport !== "http") return null;
  if (!status || (!status.required && !needsAuth)) return null;

  async function connect() {
    setBusy(true);
    try {
      const { authorize_url } = await api.connectMcpServer(server.id);
      // A full-page navigation, not a popup: the user must be able to read the
      // address bar and see whose consent screen they are on.
      window.location.href = authorize_url;
    } catch (error) {
      setError(error instanceof Error ? error.message : "Could not start authorization");
      setBusy(false);
    }
  }

  async function disconnect() {
    setBusy(true);
    try {
      await api.disconnectMcpServer(server.id);
      await onChanged();
    } finally {
      setBusy(false);
    }
  }

  // Composed from the card's existing classes rather than new ones: this row
  // sits inside the same card and should not look like a different component.
  if (status.connected) {
    return (
      <div className="mcp-card-meta">
        <span className="status-pill ready">Account connected</span>
        {status.issuer}
        <button className="ghost-button" onClick={() => void disconnect()} disabled={busy}>
          <Unlink size={14} /> Disconnect
        </button>
      </div>
    );
  }
  return (
    <div className="mcp-card-meta">
      <span className="status-pill">Sign-in required</span>
      <button className="primary-button" onClick={() => void connect()} disabled={busy}>
        <KeyRound size={14} /> {busy ? "Starting…" : "Connect"}
      </button>
      <span className="field-hint">
        This server signs each person in separately, so connecting your account
        does not give the rest of the workspace access to it.
      </span>
    </div>
  );
}

export function McpView({
  servers,
  addServer,
  refreshServer,
  setServerEnabled,
  setToolEnabled,
  removeServer,
}: McpViewProps) {
  const [adding, setAdding] = useState(false);
  const [refreshing, setRefreshing] = useState<string | null>(null);
  const [authError, setAuthError] = useState("");
  const { statuses, reload } = useMcpAuth(servers);

  async function refresh(serverId: string) {
    setRefreshing(serverId);
    try {
      await refreshServer(serverId);
      await reload();
    } finally {
      setRefreshing(null);
    }
  }

  return (
    <div className="content-page">
      <div className="page-heading">
        <h1>MCP servers</h1>
        {!adding && (
          <button className="primary-button" onClick={() => setAdding(true)}>
            <Plus size={15} /> Add server
          </button>
        )}
      </div>

      {authError && <div className="mcp-error">{authError}</div>}

      {adding && (
        <AddServerForm
          onCancel={() => setAdding(false)}
          onSubmit={async (input) => {
            await addServer(input);
            setAdding(false);
          }}
        />
      )}

      {servers.length === 0 && !adding ? (
        <div className="empty-state">
          <p>No MCP servers yet.</p>
        </div>
      ) : (
        <div className="mcp-list">
          {servers.map((server) => (
            <section key={server.id} className={`mcp-card ${server.status}`}>
              <header className="mcp-card-head">
                <div className="mcp-card-title">
                  <strong>{server.name}</strong>
                  <span className={`status-pill ${server.status}`}>{server.status}</span>
                  {!server.enabled && <span className="status-pill">disabled</span>}
                </div>
                <div className="mcp-card-actions">
                  <button
                    className="ghost-button"
                    onClick={() => void refresh(server.id)}
                    disabled={refreshing === server.id}
                  >
                    <RefreshCw size={14} />
                    {refreshing === server.id ? "Connecting…" : "Refresh tools"}
                  </button>
                  <button
                    className="ghost-button"
                    onClick={() => void setServerEnabled(server.id, !server.enabled)}
                  >
                    {server.enabled ? <X size={14} /> : <Check size={14} />}
                    {server.enabled ? "Disable" : "Enable"}
                  </button>
                  <button
                    className="icon-button"
                    onClick={() => void removeServer(server)}
                    aria-label={`Remove ${server.name}`}
                  >
                    <Trash2 size={15} />
                  </button>
                </div>
              </header>

              <div className="mcp-card-meta">
                {server.transport === "stdio"
                  ? [server.command, ...server.args].join(" ")
                  : server.url}
                {server.has_secrets && <span className="secret-pill">secrets stored</span>}
              </div>

              <AuthControls
                server={server}
                status={statuses[server.id]}
                onChanged={reload}
                setError={setAuthError}
              />

              {/* A server waiting for consent is not in an error state, and
                  repeating the 401 under the Connect button reads as one. */}
              {server.last_error && server.status !== "needs_auth" && (
                <div className="mcp-error">{server.last_error}</div>
              )}

              {server.tools.length > 0 ? (
                <ul className="mcp-tools">
                  {server.tools.map((tool) => (
                    <li key={tool.id}>
                      <label className="mcp-tool">
                        <input
                          type="checkbox"
                          checked={tool.enabled}
                          onChange={(event) =>
                            void setToolEnabled(tool.id, event.target.checked)
                          }
                        />
                        <code>{tool.name}</code>
                        <span>{tool.description}</span>
                      </label>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="mcp-hint">
                  No tools discovered yet — use Refresh tools to connect.
                </p>
              )}
            </section>
          ))}
        </div>
      )}
    </div>
  );
}
