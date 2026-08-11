"use client";

import type { AdminUsage, AdminUsageGroup } from "@workspace/api-client";
import { Info, TriangleAlert } from "lucide-react";
import { formatRelative } from "./shared";
import {
  USAGE_WINDOWS,
  barMetric,
  barShare,
  describePricing,
  formatCount,
  formatTokens,
  groupMetricValue,
  identifyRun,
  readSpend,
  shortId,
  windowLabel,
} from "./usage-format";

/**
 * What this workspace spent on models, and how much of that is actually known.
 *
 * Its own file, and its own component, for the reason the honesty rules in
 * `usage-format` exist: this is the one panel on the Admin screen where a
 * plausible-looking number can be wrong in a direction that costs money. The
 * default deployment configures no prices at all, so the common case is real
 * tokens and no cost — and every dollar on screen here is therefore printed by
 * `readSpend`, which answers "Not priced" rather than "$0.00" when it has
 * nothing measured to report.
 *
 * The layout follows the question an owner opens it with. The banner says how
 * far the numbers can be trusted; the headline row is the window's totals; the
 * costliest runs come next, because a runaway loop is the thing worth finding
 * and it is a *row*, not an aggregate; the three breakdowns are last, for the
 * follow-up question of where it went.
 */
export type UsagePanelProps = {
  usage: AdminUsage;
  days: number;
  onDaysChange: (days: number) => void;
  /**
   * run id → prompt preview, from the Runs panel's own fetch. The usage ledger
   * stores no text by design, so this is what makes a costly run identifiable
   * as something other than a uuid.
   */
  prompts: Map<string, string>;
};

/** One breakdown axis: rows with a bar for their share of the biggest one. */
function Breakdown({
  title,
  rows,
  metric,
  pricingConfigured,
  empty,
}: {
  title: string;
  rows: AdminUsageGroup[];
  metric: "cost" | "tokens";
  pricingConfigured: boolean;
  empty: string;
}) {
  const max = Math.max(0, ...rows.map((row) => groupMetricValue(row, metric)));
  return (
    <div className="usage-breakdown">
      <h3>{title}</h3>
      {rows.length === 0 ? (
        <p className="admin-empty">{empty}</p>
      ) : (
        <ul className="usage-rows">
          {rows.map((row) => {
            const spend = readSpend(row, pricingConfigured);
            return (
              <li key={row.key}>
                <div className="usage-row-head">
                  {/* A user id with no membership left, or a background call
                      billed to nobody, arrives with an empty label. */}
                  <strong title={row.key || "no attribution"}>
                    {row.label || row.key || "Background work"}
                  </strong>
                  <span
                    className={spend.kind === "unknown" ? "usage-cost muted" : "usage-cost"}
                    title={spend.detail}
                  >
                    {spend.label}
                  </span>
                </div>
                {/* Decorative: every figure it encodes is written beside it. */}
                <div className="usage-bar" aria-hidden="true">
                  <span style={{ width: `${barShare(groupMetricValue(row, metric), max)}%` }} />
                </div>
                <span className="usage-row-foot">
                  {formatTokens(row.total_tokens)} tokens · {formatCount(row.calls)} calls
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

export function UsagePanel({ usage, days, onDaysChange, prompts }: UsagePanelProps) {
  const { totals } = usage;
  const notice = describePricing(usage);
  const metric = barMetric(usage);
  const spend = readSpend(totals, usage.pricing_configured);
  const NoticeIcon = notice.tone === "clear" ? Info : TriangleAlert;
  const topRunMax = Math.max(
    0,
    ...usage.top_runs.map((run) => (metric === "cost" ? run.cost_usd : run.total_tokens)),
  );

  return (
    <section className="admin-panel usage-panel">
      <div className="panel-title">
        <div>
          <strong>Model spend</strong>
        </div>
        <select
          className="usage-window"
          aria-label="Usage window"
          value={days}
          onChange={(event) => onDaysChange(Number(event.target.value))}
        >
          {USAGE_WINDOWS.map((window) => (
            <option key={window} value={window}>
              {windowLabel(window)}
            </option>
          ))}
        </select>
      </div>

      <div className={`usage-notice ${notice.tone}`}>
        <NoticeIcon size={14} />
        <div>
          <strong>{notice.headline}</strong>
          <p>{notice.detail}</p>
          {notice.models.length > 0 && (
            <p className="usage-models">
              <span>Needs a rate:</span>
              {notice.models.map((model) => (
                <code key={model}>{model}</code>
              ))}
            </p>
          )}
        </div>
      </div>

      {totals.calls === 0 ? (
        <p className="admin-empty">
          No model call has been recorded in this window, so there is nothing to
          account for yet. It fills in the first time the assistant answers,
          a document is indexed, or a workflow runs.
        </p>
      ) : (
        <>
          <div className="admin-stats usage-totals">
            <div>
              <strong className={spend.kind === "unknown" ? "muted" : undefined}>
                {spend.label}
              </strong>
              <span>spend{spend.kind === "floor" ? " (at least)" : ""}</span>
            </div>
            <div>
              <strong>{formatTokens(totals.total_tokens)}</strong>
              <span>tokens</span>
            </div>
            <div>
              <strong>{formatCount(totals.calls)}</strong>
              <span>calls</span>
            </div>
            <div>
              <strong>{formatTokens(totals.input_tokens)}</strong>
              <span>in ({formatTokens(totals.cached_input_tokens)} cached)</span>
            </div>
            <div>
              <strong>{formatTokens(totals.output_tokens)}</strong>
              <span>out ({formatTokens(totals.reasoning_tokens)} reasoning)</span>
            </div>
          </div>
          <p className="usage-caption">{spend.detail}</p>

          <div className="usage-runs">
            <h3>Costliest runs</h3>
            {usage.top_runs.length === 0 ? (
              <p className="admin-empty">
                Nothing in this window belongs to a run — the calls came from
                indexing and other background work.
              </p>
            ) : (
              <table className="admin-table">
                <thead>
                  <tr>
                    <th scope="col">Run</th>
                    <th scope="col">Calls</th>
                    <th scope="col">Tokens</th>
                    <th scope="col">Cost</th>
                  </tr>
                </thead>
                <tbody>
                  {usage.top_runs.map((run) => {
                    const who = identifyRun(run, prompts);
                    const runSpend = readSpend(run, usage.pricing_configured);
                    const value = metric === "cost" ? run.cost_usd : run.total_tokens;
                    return (
                      <tr key={run.run_id}>
                        <td>
                          <strong title={who.title}>{who.title}</strong>
                          {/* The id is only repeated under a run we could name:
                              an unnamed one already *is* its id. */}
                          <span title={`Run ${run.run_id}`}>
                            {who.named ? `${shortId(run.run_id)} · ` : ""}
                            {formatRelative(run.last_call_at)}
                          </span>
                          <div className="usage-bar" aria-hidden="true">
                            <span style={{ width: `${barShare(value, topRunMax)}%` }} />
                          </div>
                        </td>
                        <td>{formatCount(run.calls)}</td>
                        <td>{formatTokens(run.total_tokens)}</td>
                        <td>
                          <span
                            className={
                              runSpend.kind === "unknown" ? "usage-cost muted" : "usage-cost"
                            }
                            title={runSpend.detail}
                          >
                            {runSpend.label}
                          </span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>

          <div className="usage-breakdowns">
            <Breakdown
              title="By model"
              rows={usage.by_model}
              metric={metric}
              pricingConfigured={usage.pricing_configured}
              empty="No model recorded."
            />
            <Breakdown
              title="By person"
              rows={usage.by_user}
              metric={metric}
              pricingConfigured={usage.pricing_configured}
              empty="No call in this window was attributed to a person."
            />
            <Breakdown
              title="By operation"
              rows={usage.by_operation}
              metric={metric}
              pricingConfigured={usage.pricing_configured}
              empty="No operation recorded."
            />
          </div>
          <p className="usage-caption">
            Bars show {metric === "cost" ? "cost" : "tokens"}
            {metric === "cost"
              ? "."
              : ", which is the figure that is measured here; cost is written beside each row."}
          </p>
        </>
      )}
    </section>
  );
}
