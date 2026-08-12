"use client";

import type {
  AdminLatencyStats,
  AdminObservability,
  AdminThroughputBucket,
} from "@workspace/api-client";
import { formatRelative } from "./shared";
import {
  OBS_WINDOWS,
  PERCENTILES,
  formatAge,
  formatErrorRate,
  formatMs,
  obsWindowLabel,
  share,
} from "./observability-format";

export { OBS_WINDOWS } from "./observability-format";

/**
 * How the workspace's runs behave: how fast they answer, how many there are,
 * how often they fail, what is live right now, and whether people keep coming
 * back.
 *
 * A sibling of the spend panel and built the same way — its own file, its own
 * component, `section.admin-panel` with a window `<select>` — because it asks a
 * different question of the same run history. Where spend asks "what did this
 * cost", this asks "is it healthy": the honesty rule here is the one the backend
 * enforces, that an unmeasured percentile is `null`, so it renders as a dash and
 * never as `0ms`, which would read as an instant response nobody actually saw.
 */
export type ObservabilityPanelProps = {
  observability: AdminObservability;
  hours: number;
  onHoursChange: (hours: number) => void;
};

/** One metric's distribution, as rows scaled against its own worst case. */
function LatencyMetric({ title, stats }: { title: string; stats: AdminLatencyStats }) {
  const max = stats.max_ms ?? 0;
  return (
    <div className="observability-metric">
      <div className="observability-metric-head">
        <h3>{title}</h3>
        <span>{stats.samples.toLocaleString("en-US")} runs</span>
      </div>
      {stats.samples === 0 ? (
        <p className="admin-empty">No run in this window produced a sample.</p>
      ) : (
        <ul className="usage-rows observability-percentiles">
          {PERCENTILES.map(({ key, label }) => {
            const value = stats[key];
            return (
              <li key={key}>
                <div className="usage-row-head">
                  <strong>{label}</strong>
                  <span className="observability-value">{formatMs(value)}</span>
                </div>
                <div className="usage-bar" aria-hidden="true">
                  <span style={{ width: `${share(value ?? 0, max)}%` }} />
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

/** Runs per bucket as a row of columns, height encoding the count. */
function Throughput({ buckets }: { buckets: AdminThroughputBucket[] }) {
  const max = Math.max(0, ...buckets.map((bucket) => bucket.count));
  if (max === 0) {
    return <p className="admin-empty">No run started in this window.</p>;
  }
  return (
    <div className="observability-throughput" role="img" aria-label="Runs over time">
      {buckets.map((bucket) => (
        <div
          key={bucket.start}
          className="observability-col"
          title={`${bucket.count} run${bucket.count === 1 ? "" : "s"} · ${formatRelative(
            bucket.start,
          )}`}
        >
          <span style={{ height: `${share(bucket.count, max)}%` }} />
        </div>
      ))}
    </div>
  );
}

export function ObservabilityPanel({
  observability,
  hours,
  onHoursChange,
}: ObservabilityPanelProps) {
  const {
    ttft,
    total,
    runs_in_window,
    throughput,
    completed,
    failed,
    cancelled,
    error_rate,
    screen_flags,
    recent_failures,
    live_runs,
    retention,
  } = observability;
  const errorPct = formatErrorRate(error_rate);

  return (
    <section className="admin-panel observability-panel">
      <div className="panel-title">
        <div>
          <strong>Observability</strong>
        </div>
        <select
          className="usage-window"
          aria-label="Observability window"
          value={hours}
          onChange={(event) => onHoursChange(Number(event.target.value))}
        >
          {OBS_WINDOWS.map((window) => (
            <option key={window} value={window}>
              {obsWindowLabel(window)}
            </option>
          ))}
        </select>
      </div>

      {runs_in_window === 0 ? (
        <p className="admin-empty">
          No run has started in this window, so there is no latency, throughput,
          or error rate to report yet. It fills in the first time the assistant
          answers or a workflow runs.
        </p>
      ) : (
        <>
          <div className="observability-metrics">
            <LatencyMetric title="Time to first token" stats={ttft} />
            <LatencyMetric title="Total latency" stats={total} />
          </div>

          <div className="observability-section">
            <div className="usage-runs">
              <h3>Runs over time</h3>
              <Throughput buckets={throughput} />
              <p className="usage-caption">
                {runs_in_window.toLocaleString("en-US")} run
                {runs_in_window === 1 ? "" : "s"} in this window.
              </p>
            </div>
          </div>

          <div className="admin-stats observability-health">
            <div>
              <strong className={error_rate > 0 ? "observability-error" : undefined}>
                {errorPct}
              </strong>
              <span>error rate</span>
            </div>
            <div>
              <strong>{completed.toLocaleString("en-US")}</strong>
              <span>completed</span>
            </div>
            <div>
              <strong className={failed > 0 ? "observability-error" : undefined}>
                {failed.toLocaleString("en-US")}
              </strong>
              <span>failed</span>
            </div>
            <div>
              <strong>{cancelled.toLocaleString("en-US")}</strong>
              <span>cancelled</span>
            </div>
            {/* Only when the screen caught something: a zero here would read as
                "the screen is on and quiet" on a deployment where it is simply
                off, which the absent field cannot distinguish from a real zero. */}
            {typeof screen_flags === "number" && screen_flags > 0 && (
              <div>
                <strong className="observability-error">
                  {screen_flags.toLocaleString("en-US")}
                </strong>
                <span>injections screened</span>
              </div>
            )}
          </div>

          <div className="observability-lists">
            <div className="usage-runs">
              <h3>Live now</h3>
              {live_runs.length === 0 ? (
                <p className="admin-empty">Nothing is running right now.</p>
              ) : (
                <table className="admin-table">
                  <thead>
                    <tr>
                      <th scope="col">Run</th>
                      <th scope="col">Status</th>
                      <th scope="col">Age</th>
                    </tr>
                  </thead>
                  <tbody>
                    {live_runs.map((run) => (
                      <tr key={run.run_id}>
                        <td>
                          <strong title={`Run ${run.run_id}`}>{run.agent_id}</strong>
                          <span title={`Conversation ${run.conversation_id}`}>
                            {formatRelative(run.created_at)}
                          </span>
                        </td>
                        <td>
                          <span className="admin-tag">{run.status}</span>
                        </td>
                        <td>{formatAge(run.age_seconds)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>

            <div className="usage-runs">
              <h3>Recent failures</h3>
              {recent_failures.length === 0 ? (
                <p className="admin-empty">No run has failed in this window.</p>
              ) : (
                <ul className="admin-list">
                  {recent_failures.map((run) => (
                    <li key={run.run_id}>
                      <div>
                        <strong title={`Run ${run.run_id}`}>
                          {formatRelative(run.updated_at)}
                        </strong>
                        <span title={`Conversation ${run.conversation_id}`}>
                          started {formatRelative(run.created_at)}
                        </span>
                        <small className="admin-error">{run.error}</small>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>

          <div className="observability-section">
            <div className="usage-runs">
              <h3>Active members</h3>
              <div className="admin-stats">
                <div>
                  <strong>{retention.dau.toLocaleString("en-US")}</strong>
                  <span>daily</span>
                </div>
                <div>
                  <strong>{retention.wau.toLocaleString("en-US")}</strong>
                  <span>weekly</span>
                </div>
                <div>
                  <strong>{retention.mau.toLocaleString("en-US")}</strong>
                  <span>monthly</span>
                </div>
              </div>
              <p className="usage-caption">
                Distinct members seen in the last day, week, and month — regardless
                of the window above.
              </p>
            </div>
          </div>
        </>
      )}
    </section>
  );
}
