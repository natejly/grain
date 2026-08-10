"use client";

import { Check, Plug, Plus, RefreshCw, Trash2, X } from "lucide-react";
import type { McpServer, McpServerInput } from "@workspace/api-client";
import { FormEvent, useState } from "react";

export type McpViewProps = {
  servers: McpServer[];
  addServer: (input: McpServerInput) => Promise<void>;
  refreshServer: (serverId: string) => Promise<void>;
  setServerEnabled: (serverId: string, enabled: boolean) => Promise<void>;
  setToolEnabled: (toolId: string, enabled: boolean) => Promise<void>;
  removeServer: (server: McpServer) => Promise<void>;
};

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

  async function refresh(serverId: string) {
    setRefreshing(serverId);
    try {
      await refreshServer(serverId);
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
          <Plug size={22} />
          <p>No MCP servers yet. Add one to give the agent its tools.</p>
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

              {server.last_error && <div className="mcp-error">{server.last_error}</div>}

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
