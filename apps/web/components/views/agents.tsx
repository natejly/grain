"use client";

import type { AgentInfo, ToolInfo } from "@workspace/api-client";
import { Bot, Check, Pencil, Plus, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { describeError } from "./shared";

/**
 * Author the agents the workspace talks to: a name, a system prompt, and —
 * optionally — a provisioned subset of the tool registry.
 *
 * The panel owns its own data rather than joining the workspace hook, like
 * WorkflowsView: nobody who is chatting needs the agent list at page load, and
 * the chat composer fetches the (much smaller) enabled list for itself.
 *
 * Retirement, not deletion, is the ordinary end of an agent: the API refuses
 * to delete one with run history (its past runs must stay attributable) and
 * refuses to disable or delete the last enabled agent. Both refusals arrive as
 * 409s with a sentence, and the sentence is the UI, verbatim.
 */

type AgentsViewProps = {
  setError: (message: string) => void;
};

/** "All tools" / "3 tools" / "No tools" — the card's one-line tool summary. */
function toolSummary(agent: AgentInfo): string {
  if (agent.allowed_tools === null) return "All tools";
  if (agent.allowed_tools.length === 0) return "No tools";
  const count = agent.allowed_tools.length;
  return `${count} ${count === 1 ? "tool" : "tools"}`;
}

export function AgentsView({ setError }: AgentsViewProps) {
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [loaded, setLoaded] = useState(false);
  /** null = closed; "" = creating; an id = editing that agent. */
  const [editing, setEditing] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      const [agentRows, toolRows] = await Promise.all([
        api.listAgents(),
        api.listTools(),
      ]);
      setAgents(agentRows);
      setTools(toolRows);
      setLoaded(true);
    } catch (caught) {
      setError(describeError(caught, "Could not load agents"));
    }
  }, [setError]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const setEnabled = async (agent: AgentInfo, enabled: boolean) => {
    try {
      await api.updateAgent(agent.id, { enabled });
      await reload();
    } catch (caught) {
      setError(
        describeError(
          caught,
          enabled ? "Could not enable the agent" : "Could not disable the agent",
        ),
      );
    }
  };

  const remove = async (agent: AgentInfo) => {
    // Same guard the other destructive rows have: nothing here can be undone,
    // and the button sits one slip below "Edit".
    if (!window.confirm(`Delete “${agent.name}”?`)) return;
    try {
      await api.deleteAgent(agent.id);
      await reload();
    } catch (caught) {
      setError(describeError(caught, "Could not delete the agent"));
    }
  };

  const open = editing === "" ? null : agents.find((row) => row.id === editing);

  return (
    <div className="content-page">
      <div className="page-heading">
        <h1>Agents</h1>
        <button className="primary-button" onClick={() => setEditing("")}>
          <Plus size={14} />
          New agent
        </button>
      </div>

      {loaded && agents.length === 0 ? (
        <div className="empty-state">
          <p>No agents yet.</p>
        </div>
      ) : (
        <div className="mcp-list">
          {agents.map((agent) => (
            <section
              key={agent.id}
              className={`mcp-card ${agent.enabled ? "ready" : "disabled"}`}
            >
              <header className="mcp-card-head">
                <div className="mcp-card-title">
                  <Bot size={15} aria-hidden />
                  <strong>{agent.name}</strong>
                  <span className="status-pill">{toolSummary(agent)}</span>
                  {!agent.enabled && <span className="status-pill">disabled</span>}
                </div>
                <div className="mcp-card-actions">
                  <button
                    className="ghost-button"
                    onClick={() => setEditing(agent.id)}
                  >
                    <Pencil size={14} />
                    Edit
                  </button>
                  <button
                    className="ghost-button"
                    onClick={() => void setEnabled(agent, !agent.enabled)}
                  >
                    {agent.enabled ? <X size={14} /> : <Check size={14} />}
                    {agent.enabled ? "Disable" : "Enable"}
                  </button>
                  <button
                    className="ghost-button"
                    onClick={() => void remove(agent)}
                    aria-label={`Delete ${agent.name}`}
                  >
                    Delete
                  </button>
                </div>
              </header>
              {agent.description && (
                <div className="mcp-card-meta">{agent.description}</div>
              )}
              <p className="agent-instructions">{agent.instructions}</p>
            </section>
          ))}
        </div>
      )}

      {editing !== null && (
        <AgentEditor
          agent={open ?? null}
          tools={tools}
          setError={setError}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            void reload();
          }}
        />
      )}
    </div>
  );
}

type AgentEditorProps = {
  /** null = creating a new agent. */
  agent: AgentInfo | null;
  tools: ToolInfo[];
  setError: (message: string) => void;
  onClose: () => void;
  onSaved: () => void;
};

/**
 * The drawer. Its state lives here, inside the component that only renders
 * while the drawer is open, so closing it resets a half-filled form for free.
 */
function AgentEditor({ agent, tools, setError, onClose, onSaved }: AgentEditorProps) {
  const [name, setName] = useState(agent?.name ?? "");
  const [description, setDescription] = useState(agent?.description ?? "");
  const [instructions, setInstructions] = useState(agent?.instructions ?? "");
  const [scoped, setScoped] = useState(agent !== null && agent.allowed_tools !== null);
  const [selected, setSelected] = useState<Set<string>>(
    () => new Set(agent?.allowed_tools ?? []),
  );
  const [saving, setSaving] = useState(false);

  const families = useMemo(() => {
    const grouped = new Map<string, ToolInfo[]>();
    for (const tool of tools) {
      const bucket = grouped.get(tool.family) ?? [];
      bucket.push(tool);
      grouped.set(tool.family, bucket);
    }
    return [...grouped.entries()];
  }, [tools]);

  /**
   * Granted names the live registry no longer serves (an MCP server was
   * removed, a feature turned off). Kept visible and removable, never silently
   * dropped: at run time they intersect to nothing, so they are inert — but a
   * grant a user cannot see is a grant they cannot revoke.
   */
  const unavailable = useMemo(() => {
    const live = new Set(tools.map((tool) => tool.name));
    return [...selected].filter((tool) => !live.has(tool)).sort();
  }, [selected, tools]);

  const toggle = (tool: string, checked: boolean) => {
    setSelected((current) => {
      const next = new Set(current);
      if (checked) next.add(tool);
      else next.delete(tool);
      return next;
    });
  };

  const save = async () => {
    setSaving(true);
    try {
      if (agent === null) {
        await api.createAgent({
          name,
          description,
          instructions,
          allowed_tools: scoped ? [...selected].sort() : null,
        });
      } else {
        await api.updateAgent(agent.id, {
          name,
          description,
          instructions,
          ...(scoped
            ? { allowed_tools: [...selected].sort() }
            : { clear_allowed_tools: true }),
        });
      }
      onSaved();
    } catch (caught) {
      setError(describeError(caught, "Could not save the agent"));
    } finally {
      setSaving(false);
    }
  };

  const ready = name.trim().length > 0 && instructions.trim().length > 0;

  return (
    <div className="drawer-scrim" onClick={onClose}>
      <div className="agent-drawer" onClick={(event) => event.stopPropagation()}>
        <div className="drawer-header">
          <div>
            <span>{agent === null ? "New agent" : "Edit agent"}</span>
            <strong>{name.trim() || "Unnamed agent"}</strong>
          </div>
          <button className="icon-button" onClick={onClose} aria-label="Close">
            <X size={15} />
          </button>
        </div>

        <div className="mcp-form agent-form">
          <label>
            Name
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              maxLength={100}
              aria-label="Agent name"
            />
          </label>
          <label>
            Description
            <input
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              maxLength={2000}
              aria-label="Agent description"
            />
          </label>
          <label>
            System prompt
            <textarea
              value={instructions}
              onChange={(event) => setInstructions(event.target.value)}
              rows={8}
              maxLength={20000}
              aria-label="System prompt"
            />
          </label>

          <fieldset className="agent-provisioning">
            <legend>Tools</legend>
            <label className="mcp-tool">
              <input
                type="radio"
                name="agent-tools-mode"
                checked={!scoped}
                onChange={() => setScoped(false)}
              />
              <span>All tools — everything the workspace has, present and future</span>
            </label>
            <label className="mcp-tool">
              <input
                type="radio"
                name="agent-tools-mode"
                checked={scoped}
                onChange={() => setScoped(true)}
              />
              <span>Only these</span>
            </label>

            {scoped && (
              <div className="agent-tool-groups">
                {families.map(([family, familyTools]) => (
                  <div key={family} className="agent-tool-family">
                    <h4>{family}</h4>
                    <ul className="mcp-tools">
                      {familyTools.map((tool) => (
                        <li key={tool.name}>
                          <label className="mcp-tool">
                            <input
                              type="checkbox"
                              checked={selected.has(tool.name)}
                              onChange={(event) =>
                                toggle(tool.name, event.target.checked)
                              }
                            />
                            <code>{tool.name}</code>
                            <span>
                              {tool.description}
                              {!tool.read_only && " (writes)"}
                            </span>
                          </label>
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
                {unavailable.length > 0 && (
                  <div className="agent-tool-family">
                    <h4>unavailable</h4>
                    <ul className="mcp-tools">
                      {unavailable.map((tool) => (
                        <li key={tool}>
                          <label className="mcp-tool agent-tool-unavailable">
                            <input
                              type="checkbox"
                              checked
                              onChange={() => toggle(tool, false)}
                            />
                            <code>{tool}</code>
                            <span>not in the current registry; inert until it returns</span>
                          </label>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            )}
          </fieldset>

          <div className="agent-editor-actions">
            <button className="ghost-button" onClick={onClose}>
              Cancel
            </button>
            <button
              className="primary-button"
              onClick={() => void save()}
              disabled={!ready || saving}
            >
              {saving ? "Saving…" : agent === null ? "Create agent" : "Save changes"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
