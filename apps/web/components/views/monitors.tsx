"use client";

import type {
  Dataset,
  DatasetMetric,
  Monitor,
  MonitorComparator,
} from "@workspace/api-client";
import {
  Check,
  Gauge,
  LoaderCircle,
  Play,
  Plus,
  Trash2,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import {
  COMPARATOR_LABELS,
  describeMonitorSchedule,
  lastValueCopy,
  stateLabel,
  stateTone,
  thresholdCopy,
} from "./monitor-format";
import { describeError, formatRelative } from "./shared";

/**
 * Metric monitors: a number watched on a schedule, alerting the Inbox when it
 * crosses a line.
 *
 * Structurally the Schedules view's sibling — the same list-left detail-right
 * layout, the same "a stored schedule is an intention, only an armed ticker
 * makes it a promise" honesty about dispatch — because the two ride the same
 * tick. What it deliberately does NOT have is a run inbox of its own: a trip
 * lands in the Inbox's Alerts tab, beside everything else waiting on a human,
 * so this page is where monitors are *defined*, never a second place to triage.
 *
 * The query builder is kept to the monitor's actual contract: one metric (the
 * first metric of the first row is what the evaluation reads), a comparator,
 * a threshold. A monitor needing filters or grouping can be authored through
 * the API; the form covers the "watch this column's total" case that is the
 * whole reason the feature exists.
 */
export type MonitorsViewProps = {
  setError: (message: string) => void;
  /** The workspace's datasets — the pickable subjects of a monitor. */
  datasets: Dataset[];
};

type Operation = "count" | "sum" | "avg" | "min" | "max";

/** The one metric the form builds: what the evaluation will read. */
function buildMetric(operation: Operation, field: string): DatasetMetric {
  if (operation === "count") return { operation, field: null, label: "count" };
  return { operation, field, label: `${operation}_${field}` };
}

/** Columns this operation can honestly aggregate. */
function fieldsFor(operation: Operation, dataset: Dataset | null): string[] {
  if (!dataset || operation === "count") return [];
  const numericOnly = operation === "sum" || operation === "avg";
  return dataset.columns
    .filter((column) =>
      numericOnly ? column.type === "integer" || column.type === "number" : true,
    )
    .map((column) => column.name);
}

function MonitorForm({
  datasets,
  setError,
  onCreated,
}: {
  datasets: Dataset[];
  setError: (message: string) => void;
  onCreated: (monitor: Monitor) => void;
}) {
  const [name, setName] = useState("");
  const [datasetId, setDatasetId] = useState("");
  const [operation, setOperation] = useState<Operation>("count");
  const [field, setField] = useState("");
  const [comparator, setComparator] = useState<MonitorComparator>("gt");
  const [threshold, setThreshold] = useState("");
  const [scheduleCron, setScheduleCron] = useState("");
  const [timezone, setTimezone] = useState("UTC");
  const [busy, setBusy] = useState(false);

  const dataset = datasets.find((item) => item.id === datasetId) ?? null;
  const fields = fieldsFor(operation, dataset);
  const fieldReady = operation === "count" || Boolean(field);
  const thresholdNumber = Number(threshold);
  const ready = Boolean(
    name.trim() &&
      datasetId &&
      fieldReady &&
      threshold.trim() &&
      Number.isFinite(thresholdNumber) &&
      scheduleCron.trim(),
  );

  async function submit() {
    if (!ready || busy) return;
    setBusy(true);
    try {
      const created = await api.createMonitor({
        name: name.trim(),
        dataset_id: datasetId,
        query: { metrics: [buildMetric(operation, field)], limit: 1 },
        comparator,
        threshold: thresholdNumber,
        schedule_cron: scheduleCron.trim(),
        schedule_timezone: timezone.trim() || "UTC",
      });
      onCreated(created);
    } catch (caught) {
      setError(describeError(caught, "Could not create that monitor"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="workflow-author">
      <div className="page-heading">
        <div>
          <h1>New monitor</h1>
        </div>
      </div>

      <form
        className="cron-form"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <label className="cron-field">
          <span>Name</span>
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="Daily revenue floor"
          />
        </label>

        <label className="cron-field">
          <span>Dataset</span>
          <select
            value={datasetId}
            onChange={(event) => {
              setDatasetId(event.target.value);
              // A field belongs to one dataset's schema; changing the dataset
              // must not silently keep a column it may not have.
              setField("");
            }}
          >
            <option value="">Pick a dataset…</option>
            {datasets.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
        </label>

        <div className="cron-field-row">
          <label className="cron-field">
            <span>Metric</span>
            <select
              value={operation}
              onChange={(event) => {
                setOperation(event.target.value as Operation);
                setField("");
              }}
            >
              <option value="count">Count of rows</option>
              <option value="sum">Sum of a column</option>
              <option value="avg">Average of a column</option>
              <option value="min">Minimum of a column</option>
              <option value="max">Maximum of a column</option>
            </select>
          </label>
          {operation !== "count" && (
            <label className="cron-field">
              <span>Column</span>
              <select value={field} onChange={(event) => setField(event.target.value)}>
                <option value="">Pick a column…</option>
                {fields.map((column) => (
                  <option key={column} value={column}>
                    {column}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>

        <div className="cron-field-row">
          <label className="cron-field">
            <span>Alert when the value</span>
            <select
              value={comparator}
              onChange={(event) =>
                setComparator(event.target.value as MonitorComparator)
              }
            >
              {(Object.keys(COMPARATOR_LABELS) as MonitorComparator[]).map(
                (option) => (
                  <option key={option} value={option}>
                    {COMPARATOR_LABELS[option]}
                  </option>
                ),
              )}
            </select>
          </label>
          <label className="cron-field">
            <span>Threshold</span>
            <input
              type="number"
              value={threshold}
              onChange={(event) => setThreshold(event.target.value)}
              placeholder="100"
            />
          </label>
        </div>

        <div className="cron-field-row">
          <label className="cron-field">
            <span>Schedule</span>
            <input
              value={scheduleCron}
              onChange={(event) => setScheduleCron(event.target.value)}
              placeholder="0 9 * * 1"
              // A 5-field cron expression, shown and stored verbatim. Validation
              // is the server's — it refuses a malformed one with a 422.
              spellCheck={false}
              autoCapitalize="none"
            />
          </label>
          <label className="cron-field">
            <span>Timezone</span>
            <input
              value={timezone}
              onChange={(event) => setTimezone(event.target.value)}
              placeholder="UTC"
              spellCheck={false}
              autoCapitalize="none"
            />
          </label>
        </div>

        <div className="cron-form-actions">
          <button className="primary-button" type="submit" disabled={busy || !ready}>
            {busy ? <LoaderCircle size={14} className="spin" /> : <Check size={14} />}
            {busy ? "Creating…" : "Create monitor"}
          </button>
        </div>
      </form>
    </section>
  );
}

export function MonitorsView({ setError, datasets }: MonitorsViewProps) {
  const [monitors, setMonitors] = useState<Monitor[]>([]);
  const [activeId, setActiveId] = useState("");
  const [composing, setComposing] = useState(false);
  const [schedulingEnabled, setSchedulingEnabled] = useState<boolean | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  /** The last Check now's honest outcome, cleared on the next action. */
  const [checked, setChecked] = useState("");

  const active = monitors.find((item) => item.id === activeId) ?? null;

  const load = useCallback(async () => {
    try {
      setMonitors(await api.listMonitors());
    } catch (caught) {
      setError(describeError(caught, "Could not load monitors"));
    } finally {
      setLoaded(true);
    }
  }, [setError]);

  useEffect(() => {
    void load();
  }, [load]);

  // Whether the ticker can actually evaluate a monitor, asked of the ticker
  // itself, once, and only when this workspace has one to evaluate — the same
  // probe (and the same "unanswered is unknown, never yes") as the Schedules
  // view, because monitors ride the same tick.
  const probed = useRef(false);
  useEffect(() => {
    if (monitors.length === 0 || probed.current) return;
    probed.current = true;
    void api
      .workflowSchedulingEnabled()
      .then(setSchedulingEnabled)
      .catch(() => undefined);
  }, [monitors.length]);

  function select(monitor: Monitor) {
    setComposing(false);
    setActiveId(monitor.id);
    setChecked("");
  }

  function created(monitor: Monitor) {
    setMonitors((rows) => [monitor, ...rows.filter((row) => row.id !== monitor.id)]);
    setComposing(false);
    setActiveId(monitor.id);
    setChecked("");
  }

  function replace(updated: Monitor) {
    setMonitors((rows) => rows.map((row) => (row.id === updated.id ? updated : row)));
  }

  async function setEnabled(monitor: Monitor, enabled: boolean) {
    setBusy(true);
    try {
      replace(await api.updateMonitor(monitor.id, { enabled }));
    } catch (caught) {
      setError(describeError(caught, "Could not change that monitor"));
    } finally {
      setBusy(false);
    }
  }

  async function checkNow(monitor: Monitor) {
    setBusy(true);
    setChecked("");
    try {
      const outcome = await api.runMonitorNow(monitor.id);
      if (outcome.state === "skipped") {
        setChecked(`Could not evaluate: ${outcome.reason || "the query failed"}.`);
      } else if (outcome.state === "tripped") {
        setChecked(
          `Tripped — the value is ${outcome.value_json}. The alert is in the Inbox.`,
        );
      } else {
        setChecked(`Within threshold — the value is ${outcome.value_json}.`);
      }
      // The evaluation moved last_state/last_value on the server; re-read so
      // the chip agrees with the sentence above it.
      await load();
    } catch (caught) {
      setError(describeError(caught, "Could not check that monitor now"));
    } finally {
      setBusy(false);
    }
  }

  async function remove(monitor: Monitor) {
    if (!window.confirm(`Delete “${monitor.name}”?`)) return;
    try {
      await api.deleteMonitor(monitor.id);
      setMonitors((rows) => rows.filter((row) => row.id !== monitor.id));
      if (activeId === monitor.id) setActiveId("");
    } catch (caught) {
      setError(describeError(caught, "Could not delete that monitor"));
    }
  }

  const schedule = active ? describeMonitorSchedule(active, schedulingEnabled) : null;
  const datasetName = active
    ? datasets.find((item) => item.id === active.dataset_id)?.name ?? ""
    : "";

  return (
    <div className="workflow-layout">
      <aside className="workflow-sidebar">
        <div className="workflow-sidebar-head">
          <span>Monitors</span>
          <button
            className="icon-button"
            aria-label="New monitor"
            onClick={() => {
              setComposing(true);
              setActiveId("");
              setChecked("");
            }}
          >
            <Plus size={16} />
          </button>
        </div>

        {monitors.length === 0 ? (
          <p className="workflow-empty">{loaded ? "No monitors yet." : "Loading…"}</p>
        ) : (
          <ul className="workflow-items">
            {monitors.map((monitor) => (
              <li key={monitor.id}>
                <button
                  className={
                    monitor.id === activeId ? "workflow-item active" : "workflow-item"
                  }
                  onClick={() => select(monitor)}
                >
                  <Gauge size={14} />
                  <span className="workflow-item-name">{monitor.name}</span>
                  <span
                    className={`workflow-chip ${monitor.enabled ? "active" : "disabled"}`}
                  >
                    {monitor.enabled ? "on" : "off"}
                  </span>
                </button>
                <div className="workflow-item-meta">
                  <span>{stateLabel(monitor.last_state)}</span>
                  <span>{monitor.schedule_cron || "unscheduled"}</span>
                  <span>
                    {monitor.last_dispatched_at
                      ? formatRelative(monitor.last_dispatched_at)
                      : "never checked"}
                  </span>
                </div>
              </li>
            ))}
          </ul>
        )}
      </aside>

      {composing && (
        <div className="workflow-main">
          <MonitorForm datasets={datasets} setError={setError} onCreated={created} />
        </div>
      )}

      {!composing && !active && (
        <div className="workflow-main empty">
          <div className="empty-state">
            <Gauge size={22} />
            <p>
              {loaded
                ? "Pick a monitor, or watch a new number."
                : "Loading monitors…"}
            </p>
          </div>
        </div>
      )}

      {!composing && active && (
        <div className="workflow-main">
          <header className="workflow-head">
            <div>
              <h1>{active.name}</h1>
              <p>
                {thresholdCopy(active)}
                {datasetName ? ` on “${datasetName}”.` : "."}
              </p>
            </div>
            <div className="workflow-head-actions">
              <button
                className="primary-button"
                disabled={busy}
                onClick={() => void checkNow(active)}
              >
                <Play size={14} /> Check now
              </button>
              <button
                className="ghost-button"
                disabled={busy}
                onClick={() => void setEnabled(active, !active.enabled)}
              >
                {active.enabled ? "Disable" : "Enable"}
              </button>
              <button
                className="icon-button"
                aria-label={`Delete ${active.name}`}
                onClick={() => void remove(active)}
              >
                <Trash2 size={15} />
              </button>
            </div>
          </header>

          <div className="workflow-facts">
            <span
              className={`workflow-chip ${
                stateTone(active.last_state) === "warn"
                  ? "disabled"
                  : stateTone(active.last_state) === "live"
                    ? "active"
                    : ""
              }`}
            >
              {stateLabel(active.last_state)}
            </span>
            <span className={`workflow-chip ${active.enabled ? "active" : "disabled"}`}>
              {active.enabled ? "enabled" : "disabled"}
            </span>
            {lastValueCopy(active) && <span>{lastValueCopy(active)}</span>}
            <span>
              {active.last_dispatched_at
                ? `last checked ${formatRelative(active.last_dispatched_at)}`
                : "has not checked yet"}
            </span>
          </div>

          {checked && (
            <p className="workflow-approval-note live">
              <Check size={14} />
              {checked}
            </p>
          )}

          {schedule && (
            <div className="workflow-notes">
              <div className={`workflow-note ${schedule.tone}`}>
                <strong>{schedule.headline}</strong>
                <span>{schedule.detail}</span>
              </div>
            </div>
          )}

          {/* The stored question, verbatim — a monitor you cannot read is a
              monitor you cannot trust. */}
          <blockquote className="workflow-source">
            {thresholdCopy(active)}, evaluated against the stored query{" "}
            {JSON.stringify(active.query)}.
          </blockquote>
        </div>
      )}
    </div>
  );
}
