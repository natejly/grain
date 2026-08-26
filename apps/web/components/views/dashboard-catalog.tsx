"use client";

import type {
  Dashboard,
  DashboardTemplate,
  Dataset,
} from "@workspace/api-client";
import {
  Copy,
  Link2,
  Mail,
  MessageSquareText,
  Pin,
  PinOff,
  Trash2,
} from "lucide-react";
import { useState } from "react";
import { describeDashboard, refusalLines } from "./dashboard-format";
import { FavoriteStar, type FavoritesApi } from "./favorites";

/**
 * The two surfaces that are *not* the home screen: everything this workspace
 * has, and the definitions waiting for data.
 *
 * Neither is a builder. The agent writes dashboards and templates; what a
 * person does here is decide which of them belong on their own screen, and
 * point a definition at a dataset. That is the whole product decision — the
 * agent authors, the user curates — expressed as two lists and no canvas.
 */

export type DashboardCatalogProps = {
  dashboards: Dashboard[];
  pinnedIds: Set<string>;
  pin: (dashboardId: string) => Promise<void>;
  unpin: (dashboardId: string) => Promise<void>;
  /** Copy the definition under "<name> copy" — editing without losing the original. */
  duplicate: (dashboard: Dashboard) => Promise<void>;
  remove: (dashboard: Dashboard) => Promise<void>;
  open: (dashboardId: string) => void;
  /** Open the shell's comments drawer about this dashboard. */
  comment?: (dashboard: Dashboard) => void;
  /** Open the share-links modal: public read-only URLs for this dashboard. */
  share?: (dashboard: Dashboard) => void;
  /** Open the subscribe modal: scheduled snapshot mail of this dashboard. */
  subscribe?: (dashboard: Dashboard) => void;
  /** The shell's one favorites list; without it the rows offer no star. */
  favorites?: FavoritesApi;
};

/**
 * Every dashboard in the workspace, as a page section rather than a popover.
 *
 * It was a dropdown for a while, which made the full inventory a thing you
 * summon and lose on every click-away — and made "where do dashboards LIVE"
 * answerable only by memory. A list that is simply on the page under the
 * pinned grid is the honest shape: your screen first, the shelf below it.
 */
export function DashboardCatalog({
  dashboards,
  pinnedIds,
  pin,
  unpin,
  duplicate,
  remove,
  open,
  comment,
  share,
  subscribe,
  favorites,
}: DashboardCatalogProps) {
  return (
    <section className="dashboard-catalog" aria-label="All dashboards">
      <h2>
        All dashboards
        <span className="nav-count">{dashboards.length}</span>
      </h2>
      {dashboards.length === 0 ? (
        <p className="section-note">None yet. Ask the agent for a chart.</p>
      ) : (
        <ul className="dashboard-catalog-list">
          {dashboards.map((dashboard) => {
            const pinned = pinnedIds.has(dashboard.id);
            return (
              <li key={dashboard.id}>
                <button
                  type="button"
                  className="dashboard-catalog-open"
                  onClick={() => open(dashboard.id)}
                >
                  <strong>{dashboard.name}</strong>
                  <span>{describeDashboard(dashboard)}</span>
                </button>
                {/* Beside Pin, wearing its aria pattern: the verb is the
                    outcome, aria-pressed is the state. Pin is "on my home
                    screen"; the star is "in my sidebar, from anywhere". */}
                {favorites && (
                  <FavoriteStar
                    kind="dashboard"
                    targetId={dashboard.id}
                    label={dashboard.name}
                    favorites={favorites}
                  />
                )}
                <button
                  type="button"
                  className="icon-button"
                  aria-label={`${pinned ? "Unpin" : "Pin"} ${dashboard.name}`}
                  aria-pressed={pinned}
                  onClick={() => void (pinned ? unpin : pin)(dashboard.id)}
                >
                  {pinned ? <PinOff size={14} /> : <Pin size={14} />}
                </button>
                <button
                  type="button"
                  className="icon-button"
                  aria-label={`Duplicate ${dashboard.name}`}
                  title="Copy this dashboard under a new name"
                  onClick={() => void duplicate(dashboard)}
                >
                  <Copy size={14} />
                </button>
                {comment && (
                  <button
                    type="button"
                    className="icon-button"
                    aria-label={`Comments on ${dashboard.name}`}
                    title="Comments on this dashboard"
                    onClick={() => comment(dashboard)}
                  >
                    <MessageSquareText size={14} />
                  </button>
                )}
                {share && (
                  <button
                    type="button"
                    className="icon-button"
                    aria-label={`Share ${dashboard.name}`}
                    title="Share this dashboard with a public link"
                    onClick={() => share(dashboard)}
                  >
                    <Link2 size={14} />
                  </button>
                )}
                {subscribe && (
                  <button
                    type="button"
                    className="icon-button"
                    aria-label={`Subscribe to ${dashboard.name}`}
                    title="Email this dashboard on a schedule"
                    onClick={() => subscribe(dashboard)}
                  >
                    <Mail size={14} />
                  </button>
                )}
                <button
                  type="button"
                  className="icon-button"
                  aria-label={`Delete ${dashboard.name}`}
                  onClick={() => void remove(dashboard)}
                >
                  <Trash2 size={14} />
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

export type TemplateBindProps = {
  templates: DashboardTemplate[];
  datasets: Dataset[];
  bind: (
    template: DashboardTemplate,
    payload: {
      name: string;
      dataset_id: string;
      column_bindings: Record<string, string>;
    },
  ) => Promise<Dashboard>;
};

/**
 * Requirement 4: a binding is decided here, in front of whoever asked for it.
 *
 * The selects are pre-filled by exact name match and *not* by guesswork beyond
 * that: a template requiring `region` against a dataset with no `region` shows
 * an unset select, the bind is attempted, and the API answers with a sentence
 * naming the column and the types it would accept. Guessing a plausible column
 * would produce a dashboard that renders — with the wrong data in it — which is
 * the failure this whole path exists to prevent.
 */
function TemplateCard({
  template,
  datasets,
  bind,
}: {
  template: DashboardTemplate;
  datasets: Dataset[];
  bind: TemplateBindProps["bind"];
}) {
  const [datasetId, setDatasetId] = useState(datasets[0]?.id ?? "");
  const [name, setName] = useState(template.name);
  const [bindings, setBindings] = useState<Record<string, string>>({});
  const [problems, setProblems] = useState<string[]>([]);
  const [bound, setBound] = useState("");
  const [busy, setBusy] = useState(false);
  const dataset = datasets.find((item) => item.id === datasetId) ?? null;

  function chosen(column: string): string {
    if (bindings[column] !== undefined) return bindings[column];
    return dataset?.columns.some((item) => item.name === column) ? column : "";
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setProblems([]);
    setBound("");
    setBusy(true);
    try {
      const dashboard = await bind(template, {
        name: name.trim() || template.name,
        dataset_id: datasetId,
        column_bindings: Object.fromEntries(
          template.required_columns
            .map((column) => [column.name, chosen(column.name)] as const)
            .filter(([, actual]) => actual !== ""),
        ),
      });
      setBound(dashboard.name);
    } catch (caught) {
      const lines = refusalLines(caught);
      setProblems(lines.length ? lines : ["That binding was refused."]);
    } finally {
      setBusy(false);
    }
  }

  const selectId = (column: string) => `bind-${template.id}-${column}`;

  return (
    <form className="dashboard-template-card" onSubmit={(event) => void submit(event)}>
      <div className="dashboard-template-head">
        <strong>{template.name}</strong>
        {template.description && <span>{template.description}</span>}
      </div>

      <label className="field">
        <span>Dashboard name</span>
        <input
          value={name}
          onChange={(event) => setName(event.target.value)}
          aria-label={`Name for a dashboard from ${template.name}`}
        />
      </label>

      <label className="field">
        <span>Dataset</span>
        <select
          value={datasetId}
          onChange={(event) => {
            setDatasetId(event.target.value);
            // The old map named columns of a different dataset; keeping it
            // would silently bind to names that are no longer there.
            setBindings({});
            setProblems([]);
          }}
          aria-label={`Dataset for ${template.name}`}
        >
          {datasets.length === 0 && <option value="">No datasets yet</option>}
          {datasets.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
            </option>
          ))}
        </select>
      </label>

      <div className="dashboard-template-columns">
        {template.required_columns.map((column) => (
          <label className="field" key={column.name} htmlFor={selectId(column.name)}>
            <span>
              {column.name}
              <em>{column.type}</em>
            </span>
            <select
              id={selectId(column.name)}
              value={chosen(column.name)}
              onChange={(event) =>
                setBindings((current) => ({
                  ...current,
                  [column.name]: event.target.value,
                }))
              }
            >
              <option value="">Choose a column…</option>
              {(dataset?.columns ?? []).map((item) => (
                <option key={item.name} value={item.name}>
                  {item.name} ({item.type})
                </option>
              ))}
            </select>
          </label>
        ))}
      </div>

      {problems.length > 0 && (
        <ul className="dashboard-bind-refusal" role="alert">
          {problems.map((problem) => (
            <li key={problem}>{problem}</li>
          ))}
        </ul>
      )}
      {bound && (
        <p className="dashboard-bind-ok" role="status">
          Created “{bound}”. Pin it from All dashboards.
        </p>
      )}

      <button className="primary-button" type="submit" disabled={busy || !datasetId}>
        {busy ? "Binding…" : "Create dashboard"}
      </button>
    </form>
  );
}

export function DashboardTemplates({ templates, datasets, bind }: TemplateBindProps) {
  if (templates.length === 0) return null;
  return (
    <section className="dashboard-templates" aria-label="Dashboard templates">
      <h2>Templates</h2>
      <p className="section-note">
        A definition with no data yet. Point it at a dataset that has the columns
        it declares.
      </p>
      <div className="dashboard-template-grid">
        {templates.map((template) => (
          <TemplateCard
            key={template.id}
            template={template}
            datasets={datasets}
            bind={bind}
          />
        ))}
      </div>
    </section>
  );
}
