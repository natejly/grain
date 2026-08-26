"use client";

import type { Org, OrgPolicy } from "@workspace/api-client";
import { ApiError } from "@workspace/api-client";
import { Trash2 } from "lucide-react";
import { type FormEvent, useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { describeError } from "./shared";

/**
 * The organization: the tier above this workspace, and the one its owner cannot
 * overrule.
 *
 * Everything else on the Admin screen is something an owner *decides*. This
 * panel is mostly something an owner is *subject to*, and it is here rather than
 * on a screen of its own for that reason: the rule that denies you a tool
 * belongs next to the rules you wrote yourself, or you spend an afternoon
 * looking for the workspace policy that is not there.
 *
 * Two access levels, from one API:
 *
 * - anyone the org governs may read the posture — a policy you cannot see is
 *   indistinguishable from a bug, and that is worse for everyone than showing a
 *   list of tool names;
 * - only an org admin may change it, and a workspace owner is not one by virtue
 *   of being an owner.
 *
 * `your_role` drives which of those this renders, and the server re-checks every
 * write regardless: the disabled control is a courtesy, the 403 is the control.
 */

/** The three verdicts, strictest last — the order they are argued about in. */
const POLICIES = ["allow", "ask", "deny"] as const;
const SCOPES = ["chat", "workflow"] as const;

export type OrganizationPanelProps = {
  setError: (message: string) => void;
};

/** A comma-separated field to a list, dropping the blanks a trailing comma makes. */
function parseList(raw: string): string[] {
  return raw
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

export function OrganizationPanel({ setError }: OrganizationPanelProps) {
  const [org, setOrg] = useState<Org | null>(null);
  const [policies, setPolicies] = useState<OrgPolicy[]>([]);
  const [busy, setBusy] = useState("");
  const [problem, setProblem] = useState("");
  const [modelBound, setModelBound] = useState("");
  const [boundModels, setBoundModels] = useState(false);
  const [tool, setTool] = useState("");
  const [policy, setPolicy] = useState<string>("deny");
  const [scope, setScope] = useState<string>("chat");

  const load = useCallback(async () => {
    try {
      const [loadedOrg, loadedPolicies] = await Promise.all([
        api.getOrg(),
        api.listOrgPolicies(),
      ]);
      setOrg(loadedOrg);
      setPolicies(loadedPolicies);
      setBoundModels(loadedOrg.allowed_models !== null);
      setModelBound((loadedOrg.allowed_models ?? []).join(", "));
    } catch (caught) {
      // This panel fetches on its own rather than joining the admin view's
      // `Promise.all`, because a failure here must not replace the whole screen
      // with the owner-only sentence: an org read is not owner-gated, so a 403
      // from it would be saying something quite different.
      setError(describeError(caught, "Could not load the organization"));
    }
  }, [setError]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!org) return null;
  const admin = org.your_role === "admin";

  async function saveBounds(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setProblem("");
    setBusy("bounds");
    try {
      // "Unrestricted" means *no bound at all*, which is not the same as an
      // empty list — an empty list permits nothing. The API takes them as two
      // different requests, and this is where the choice becomes one of them.
      const saved = await api.setOrgConfig(
        boundModels
          ? { allowed_models: parseList(modelBound) }
          : { clear_model_bound: true },
      );
      setOrg(saved);
    } catch (caught) {
      setProblem(describeError(caught, "Could not save that bound"));
    } finally {
      setBusy("");
    }
  }

  async function addPolicy(event: FormEvent) {
    event.preventDefault();
    if (busy || !tool.trim()) return;
    setProblem("");
    setBusy("policy");
    try {
      const saved = await api.setOrgPolicy(tool.trim(), policy, scope);
      setPolicies((rows) => [
        ...rows.filter(
          (row) => !(row.tool_name === saved.tool_name && row.scope === saved.scope),
        ),
        saved,
      ]);
      setTool("");
    } catch (caught) {
      setProblem(describeError(caught, "Could not set that ceiling"));
    } finally {
      setBusy("");
    }
  }

  async function removePolicy(row: OrgPolicy) {
    setBusy(`${row.scope}:${row.tool_name}`);
    try {
      await api.clearOrgPolicy(row.scope, row.tool_name);
      setPolicies((rows) =>
        rows.filter(
          (other) => !(other.tool_name === row.tool_name && other.scope === row.scope),
        ),
      );
    } catch (caught) {
      // A 403 here means the caller reads this org but does not administer it,
      // which the disabled buttons already say — so it is shown as itself.
      const message =
        caught instanceof ApiError && caught.forbidden
          ? "Only an organization admin can change this."
          : describeError(caught, "Could not clear that ceiling");
      setProblem(message);
    } finally {
      setBusy("");
    }
  }

  return (
    <section className="admin-panel">
      <div className="panel-title">
        <div>
          <strong>Organization</strong>
          <span>{org.name}</span>
        </div>
        <span className="admin-tag">{org.your_role || "not a member"}</span>
      </div>

      <p className="field-hint">
        {admin
          ? "These bounds and ceilings apply to every workspace in this organization. A workspace or a person may tighten them; nothing below can loosen them."
          : "Set by an organization admin. A workspace can tighten anything here, but nothing in this workspace can loosen it — including an owner, an “always allow”, or an approval bypass."}
      </p>

      <div className="admin-stats">
        <div>
          <strong>{org.effective_models.length}</strong>
          <span>models available</span>
        </div>
        <div>
          <strong>{org.effective_harnesses.length}</strong>
          <span>harnesses available</span>
        </div>
        <div>
          <strong>{policies.length}</strong>
          <span>tool ceilings</span>
        </div>
      </div>

      <form className="budget-form" onSubmit={(event) => void saveBounds(event)}>
        <p className="budget-form-lead">
          Which models this organization allows. Unrestricted means the
          deployment&rsquo;s own list stands.
        </p>
        <div className="budget-fields">
          {/* A select rather than a checkbox, for two reasons. The form styles
              its inputs to fill their track, which a checkbox does badly; and
              the two states deserve naming — "unrestricted" and "restricted to
              a list" are the `null` and the `[]`-or-more the API distinguishes,
              and a tickbox leaves the unticked meaning to be inferred. */}
          <label htmlFor="org-bound-models">
            Model policy
            <select
              id="org-bound-models"
              className="usage-window"
              value={boundModels ? "list" : "any"}
              disabled={!admin || busy === "bounds"}
              onChange={(event) => setBoundModels(event.target.value === "list")}
            >
              <option value="any">Unrestricted</option>
              <option value="list">Restrict to a list</option>
            </select>
          </label>
          <label htmlFor="org-models">
            Allowed models
            <input
              id="org-models"
              type="text"
              value={modelBound}
              placeholder="gpt-5.6-sol, gpt-5-nano"
              disabled={!admin || !boundModels || busy === "bounds"}
              onChange={(event) => setModelBound(event.target.value)}
            />
          </label>
        </div>
        {admin && (
          <div className="budget-form-actions">
            <button type="submit" className="primary-button" disabled={busy === "bounds"}>
              Save bounds
            </button>
          </div>
        )}
      </form>

      <div className="admin-table-scroll">
        <table className="admin-table">
          <thead>
            <tr>
              <th scope="col">Tool</th>
              <th scope="col">Scope</th>
              <th scope="col">Ceiling</th>
              <th scope="col">
                <span className="visually-hidden">Clear</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {policies.map((row) => (
              <tr key={`${row.scope}:${row.tool_name}`}>
                <td>
                  <strong>{row.tool_name}</strong>
                </td>
                <td>{row.scope}</td>
                <td>
                  <span className="admin-tag">{row.policy}</span>
                </td>
                <td>
                  <button
                    className="ghost-button"
                    disabled={!admin || busy === `${row.scope}:${row.tool_name}`}
                    aria-label={`Clear the ${row.scope} ceiling for ${row.tool_name}`}
                    onClick={() => void removePolicy(row)}
                  >
                    <Trash2 size={12} /> Clear
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {policies.length === 0 && (
        <p className="admin-empty">
          No ceilings set. Every tool is decided by this workspace.
        </p>
      )}

      {admin && (
        <form className="budget-form" onSubmit={(event) => void addPolicy(event)}>
          <div className="budget-fields">
            <label htmlFor="org-tool">
              Tool
              <input
                id="org-tool"
                type="text"
                value={tool}
                placeholder="send_email"
                onChange={(event) => setTool(event.target.value)}
              />
            </label>
            <label htmlFor="org-scope">
              Scope
              <select
                id="org-scope"
                className="usage-window"
                value={scope}
                onChange={(event) => setScope(event.target.value)}
              >
                {SCOPES.map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </label>
            <label htmlFor="org-policy">
              Ceiling
              <select
                id="org-policy"
                className="usage-window"
                value={policy}
                onChange={(event) => setPolicy(event.target.value)}
              >
                {POLICIES.map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="budget-form-actions">
            <button type="submit" className="primary-button" disabled={busy === "policy"}>
              Set ceiling
            </button>
          </div>
        </form>
      )}

      {problem && (
        <p className="budget-problem" role="alert">
          {problem}
        </p>
      )}
    </section>
  );
}
