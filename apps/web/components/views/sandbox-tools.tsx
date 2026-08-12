"use client";

import { Check, Globe, Plus, ShieldAlert, Trash2, X } from "lucide-react";
import type { SandboxTool, SandboxToolInput } from "@workspace/api-client";
import { FormEvent, useState } from "react";

export type SandboxToolsViewProps = {
  tools: SandboxTool[];
  addTool: (input: SandboxToolInput) => Promise<void>;
  setToolEnabled: (toolId: string, enabled: boolean) => Promise<void>;
  removeTool: (tool: SandboxTool) => Promise<void>;
};

/** One argv token per line, trimmed; blank lines dropped. */
export function linesToList(raw: string): string[] {
  return raw
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean);
}

/**
 * Parse the schema textarea into a JSON object, or null when it is not one.
 *
 * The model is handed this verbatim as the tool's input schema, so anything but
 * a JSON object is meaningless to it. Rejecting a JSON array, a bare scalar, or
 * `null` here — not just a parse error — points the typo at the textarea it came
 * from instead of surfacing later as an opaque 422 from the API.
 */
export function parseInputSchema(raw: string): Record<string, unknown> | null {
  let value: unknown;
  try {
    value = JSON.parse(raw || "{}");
  } catch {
    return null;
  }
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return null;
  }
  return value as Record<string, unknown>;
}

const DEFAULT_SCHEMA = '{\n  "type": "object",\n  "properties": {}\n}';

function AddToolForm({
  onSubmit,
  onCancel,
  setError,
}: {
  onSubmit: (input: SandboxToolInput) => Promise<void>;
  onCancel: () => void;
  setError: (message: string) => void;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [argv, setArgv] = useState("");
  const [schema, setSchema] = useState(DEFAULT_SCHEMA);
  const [egress, setEgress] = useState("");
  const [approval, setApproval] = useState<"inherit" | "always">("inherit");
  const [enabled, setEnabled] = useState(true);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    // Parse the schema here rather than at send: a JSON typo should point at
    // the textarea it came from, not surface as an opaque 422 from the API.
    const parsed = parseInputSchema(schema);
    if (!parsed) {
      setError("Input schema must be a JSON object.");
      return;
    }
    setBusy(true);
    try {
      await onSubmit({
        name: name.trim(),
        description: description.trim(),
        input_schema: parsed,
        // One token per line, deliberately not shell-split: the backend runs
        // these as argv and quotes each token, so a token with spaces is one
        // argument, not several.
        argv: linesToList(argv),
        // Bare hostnames, one per line — the only hosts this tool may reach.
        egress_hosts: linesToList(egress),
        approval,
        enabled,
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
            placeholder="lowercase-slug"
            required
          />
        </label>
        <label>
          Approval
          <select
            value={approval}
            onChange={(event) => setApproval(event.target.value as "inherit" | "always")}
          >
            <option value="inherit">Inherit workspace policy</option>
            <option value="always">Always ask for approval</option>
          </select>
        </label>
      </div>

      <label>
        Description
        <textarea
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          rows={2}
          placeholder="What this tool does — shown to the model."
        />
      </label>

      <label>
        Command (one argv token per line)
        <textarea
          value={argv}
          onChange={(event) => setArgv(event.target.value)}
          rows={4}
          placeholder={"curl\n-sS\nhttps://{{host}}/{{path}}"}
          required
        />
      </label>

      <label>
        Input schema (JSON Schema shown to the model)
        <textarea
          value={schema}
          onChange={(event) => setSchema(event.target.value)}
          rows={6}
        />
      </label>

      <label>
        Egress hosts (one hostname per line — the only hosts this tool may reach)
        <textarea
          value={egress}
          onChange={(event) => setEgress(event.target.value)}
          rows={2}
          placeholder={"api.example.com"}
        />
      </label>

      <label className="mcp-tool">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(event) => setEnabled(event.target.checked)}
        />
        <span>Enabled — the agent may call this tool</span>
      </label>

      <div className="mcp-form-actions">
        <button type="button" className="ghost-button" onClick={onCancel}>
          Cancel
        </button>
        <button type="submit" className="primary-button" disabled={busy}>
          Add tool
        </button>
      </div>
    </form>
  );
}

/**
 * The network line for a tool's card.
 *
 * Egress is load-bearing to reviewing what a custom tool can do, so it is shown
 * on the card the way the approval prompt shows it: no hosts declared means the
 * tool runs with no network at all, which is the strictest and safest state.
 */
export function egressLabel(hosts: string[]): string {
  if (hosts.length === 0) return "No network";
  return `Reaches ${hosts.join(", ")}`;
}

export function SandboxToolsView({
  tools,
  addTool,
  setToolEnabled,
  removeTool,
}: SandboxToolsViewProps) {
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState("");

  return (
    <div className="content-page">
      <div className="page-heading">
        <h1>Sandbox tools</h1>
        {!adding && (
          <button className="primary-button" onClick={() => setAdding(true)}>
            <Plus size={15} /> Add tool
          </button>
        )}
      </div>

      <p className="mcp-hint">
        Custom tools the agent can call, each run in the session sandbox under
        its own network egress and approval policy. A tool only ever reaches the
        hosts you list here, and never the sandbox&rsquo;s blocked internal
        addresses.
      </p>

      {error && <div className="mcp-error">{error}</div>}

      {adding && (
        <AddToolForm
          setError={setError}
          onCancel={() => {
            setError("");
            setAdding(false);
          }}
          onSubmit={async (input) => {
            setError("");
            await addTool(input);
            setAdding(false);
          }}
        />
      )}

      {tools.length === 0 && !adding ? (
        <div className="empty-state">
          <p>No sandbox tools yet.</p>
        </div>
      ) : (
        <div className="mcp-list">
          {tools.map((tool) => (
            <section key={tool.id} className="mcp-card">
              <header className="mcp-card-head">
                <div className="mcp-card-title">
                  <strong>
                    <code>{tool.name}</code>
                  </strong>
                  {tool.approval === "always" && (
                    <span className="status-pill">
                      <ShieldAlert size={12} /> always asks
                    </span>
                  )}
                  {!tool.enabled && <span className="status-pill">disabled</span>}
                </div>
                <div className="mcp-card-actions">
                  <button
                    className="ghost-button"
                    onClick={() => void setToolEnabled(tool.id, !tool.enabled)}
                  >
                    {tool.enabled ? <X size={14} /> : <Check size={14} />}
                    {tool.enabled ? "Disable" : "Enable"}
                  </button>
                  <button
                    className="icon-button"
                    onClick={() => void removeTool(tool)}
                    aria-label={`Remove ${tool.name}`}
                  >
                    <Trash2 size={15} />
                  </button>
                </div>
              </header>

              {tool.description && (
                <div className="mcp-card-meta">{tool.description}</div>
              )}

              <div className="mcp-card-meta">
                <code>{tool.argv.join(" ")}</code>
              </div>

              <div className="mcp-card-meta">
                <span className="status-pill">
                  <Globe size={12} /> {egressLabel(tool.egress_hosts)}
                </span>
              </div>
            </section>
          ))}
        </div>
      )}
    </div>
  );
}
