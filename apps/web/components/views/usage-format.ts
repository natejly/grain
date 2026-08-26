import type { AdminUsage, AdminUsageGroup, AdminUsageRun } from "@workspace/api-client";

/**
 * How the spend panel says numbers, and — the part that matters — how it says
 * the numbers it does not have.
 *
 * `MODEL_PRICES` ships empty, so on most deployments `pricing_configured` is
 * false: every `cost_usd` the API sums is zero by arithmetic while the token
 * counts beside it are real measurements. Rendering that as `$0.00` would be
 * indistinguishable from "we spent nothing", which is the single most misleading
 * thing this screen could say, and it would say it on day one of every install.
 *
 * So no component here formats money on its own. Every dollar figure on the
 * panel goes through `readSpend`, which is handed the slice *and* whether
 * pricing exists, and which answers "unknown" rather than a number whenever the
 * number would be a guess. Same for the partial case: with some calls priced and
 * some not, the sum is a floor and is labelled as one.
 *
 * It lives in its own file because that rule is a rule, and a rule belongs
 * somewhere a test can hold it still.
 */

/** The windows the panel offers, in days. The API accepts 1–365. */
export const USAGE_WINDOWS = [1, 7, 30, 90, 365] as const;

export function windowLabel(days: number): string {
  if (days === 1) return "Last 24 hours";
  if (days === 365) return "Last year";
  if (days % 30 === 0 && days >= 60) return `Last ${days / 30} months`;
  return `Last ${days} days`;
}

/** Lower case, for use inside a sentence. */
export function windowPhrase(days: number): string {
  const label = windowLabel(days);
  return `the ${label[0].toLowerCase()}${label.slice(1)}`;
}

/**
 * A whole count with thousands separators. Pinned to en-US rather than the
 * viewer's locale so that "1,234,567" is what the tests assert and what every
 * screenshot shows; these are engineering figures, not prose.
 */
export function formatCount(value: number): string {
  if (!Number.isFinite(value)) return "0";
  return Math.round(value).toLocaleString("en-US");
}

/** Tokens are counts; the alias exists so call sites read as what they mean. */
export const formatTokens = formatCount;

/**
 * Dollars at a precision that suits the magnitude. Sub-cent totals are normal
 * here — one embedding call costs a few thousandths of a cent — so rounding
 * everything to two places would turn most real spend into $0.00, which is the
 * error this whole module exists to avoid.
 *
 * Never call this for a figure that might not be measured: `readSpend` decides
 * that, and calls this only once it knows there is something to show.
 */
export function formatUsd(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "$0.00";
  if (value < 0.0001) return "<$0.0001";
  if (value < 0.01) return `$${value.toFixed(4)}`;
  if (value < 1000) return `$${value.toFixed(2)}`;
  // Cents stop being informative once the bill is four figures.
  return `$${Math.round(value).toLocaleString("en-US")}`;
}

export function plural(count: number, word: string): string {
  return `${formatCount(count)} ${word}${count === 1 ? "" : "s"}`;
}

/** A slice of spend: the totals, a breakdown row, or one run. */
export type SpendSlice = Pick<AdminUsageGroup, "calls" | "cost_usd" | "unpriced_calls">;

export type SpendReading = {
  /**
   * exact: every call in the slice had a rate.
   * floor: some did; the figure is a lower bound and is marked with a "+".
   * unknown: none did, so there is no figure at all.
   */
  kind: "exact" | "floor" | "unknown";
  /** What to print where a cost goes. Never "$0.00" unless it was measured. */
  label: string;
  /** One sentence saying how complete the label is. Always non-empty. */
  detail: string;
};

export function readSpend(slice: SpendSlice, pricingConfigured: boolean): SpendReading {
  const priced = Math.max(0, slice.calls - slice.unpriced_calls);
  if (!pricingConfigured || priced === 0) {
    return {
      kind: "unknown",
      label: "Not priced",
      detail: pricingConfigured
        ? `No rate is configured for the ${plural(slice.calls, "call")} here, so the cost is unknown.`
        : "No model rates are configured on this deployment, so cost cannot be computed.",
    };
  }
  if (slice.unpriced_calls > 0) {
    return {
      kind: "floor",
      label: `${formatUsd(slice.cost_usd)}+`,
      detail: `At least this much: ${plural(priced, "call")} priced, ${formatCount(
        slice.unpriced_calls,
      )} on models with no rate.`,
    };
  }
  return {
    kind: "exact",
    label: formatUsd(slice.cost_usd),
    detail: `All ${plural(slice.calls, "call")} in this slice were priced.`,
  };
}

export type PricingNotice = {
  /** off: no rates at all. gaps: some. clear: nothing to warn about. */
  tone: "off" | "gaps" | "clear";
  headline: string;
  detail: string;
  /** Models that need a rate before the figures above can be complete. */
  models: string[];
};

/**
 * The banner at the top of the panel: what the cost figures are worth, and what
 * an operator would have to do to make them worth more.
 *
 * Written as an instruction rather than a disclaimer. "Pricing not configured"
 * alone leaves the reader with a screen they cannot trust and no next step;
 * naming MODEL_PRICES and the exact models seen in the window turns it into a
 * task.
 */
export function describePricing(
  usage: Pick<AdminUsage, "pricing_configured" | "unpriced_models" | "totals" | "window_days">,
): PricingNotice {
  const { totals } = usage;
  if (!usage.pricing_configured) {
    return {
      tone: "off",
      headline: "Pricing is not configured — costs are unknown, not zero",
      detail:
        totals.calls === 0
          ? "Set MODEL_PRICES on the API to a rate per million tokens for each model this workspace calls, and spend will appear here alongside the token counts."
          : `The ${plural(totals.calls, "call")} below really happened and their tokens are measured. What they cost cannot be shown until MODEL_PRICES on the API names a rate per million tokens for each model.`,
      models: usage.unpriced_models,
    };
  }
  if (totals.calls === 0) {
    return {
      tone: "clear",
      headline: `No model call was recorded in ${windowPhrase(usage.window_days)}`,
      detail: "Rates are configured, so spend will be shown as soon as there is any.",
      models: [],
    };
  }
  if (totals.unpriced_calls > 0) {
    return {
      tone: "gaps",
      headline: `${plural(totals.unpriced_calls, "call")} ran on a model with no configured rate`,
      detail: `Every dollar figure here is a floor, not the bill: it covers ${plural(
        totals.priced_calls,
        "call",
      )} out of ${formatCount(totals.calls)}. Add the models below to MODEL_PRICES to close the gap.`,
      models: usage.unpriced_models,
    };
  }
  return {
    tone: "clear",
    headline: "Every call in this window is priced",
    detail: `${plural(totals.calls, "call")} at configured rates, ${formatTokens(
      totals.total_tokens,
    )} tokens in total.`,
    models: [],
  };
}

/**
 * What the bars measure.
 *
 * Cost only when every call in the window has a rate — otherwise a bar drawn
 * from `cost_usd` would render an unpriced model as a stub next to a priced one
 * and invite exactly the wrong conclusion about which is the expensive one.
 * Tokens are always measured, so they are the honest fallback; the panel says
 * which of the two it is using.
 */
export function barMetric(
  usage: Pick<AdminUsage, "pricing_configured" | "totals">,
): "cost" | "tokens" {
  const priced =
    usage.pricing_configured &&
    usage.totals.unpriced_calls === 0 &&
    usage.totals.cost_usd > 0;
  return priced ? "cost" : "tokens";
}

export function groupMetricValue(row: AdminUsageGroup, metric: "cost" | "tokens"): number {
  return metric === "cost" ? row.cost_usd : row.total_tokens;
}

/**
 * A row's share of the largest row, as a percentage width.
 *
 * Floored at 2 so a real but tiny row is still a visible mark: a bar of width
 * zero says "none", and none is not what a row with calls in it means.
 */
export function barShare(value: number, max: number): number {
  if (!Number.isFinite(value) || !Number.isFinite(max) || max <= 0 || value <= 0) return 0;
  return Math.max(2, Math.min(100, Math.round((value / max) * 100)));
}

/**
 * Enough of an id to match a row against a log line.
 *
 * A uuid's first group is eight hex characters and is the conventional short
 * form. Anything else keeps as much of itself as fits, because taking the first
 * dash-separated segment of a non-uuid id turns "run-runaway-0001" into "run",
 * which identifies nothing.
 */
export function shortId(id: string): string {
  const head = id.split("-")[0] ?? "";
  if (head.length >= 8) return head;
  return id.length <= 12 ? id : id.slice(0, 12).replace(/-$/, "");
}

/**
 * Name a by-agent breakdown row.
 *
 * The API labels a live agent with its name and falls back to the raw id for
 * one that has since been deleted — its spend already happened and keeps its
 * key. A bare uuid is a poor row title, so a label that *is* the key is worn
 * as "Agent <short id>", and the "" key (embeddings, ingest, compiles — calls
 * no agent made) says so in words.
 */
export function agentLabel(row: Pick<AdminUsageGroup, "key" | "label">): string {
  if (!row.key) return "Background work";
  if (!row.label || row.label === row.key) return `Agent ${shortId(row.key)}`;
  return row.label;
}

export type RunIdentity = {
  /** What to read: the run's own prompt where we have it, else its id. */
  title: string;
  /** True when the title is a prompt rather than an opaque id. */
  named: boolean;
  /** Always the full run id, for the row's title attribute. */
  runId: string;
};

/**
 * Name a costly run.
 *
 * `GET /api/admin/usage` reports ids only — the ledger holds no content by
 * design — but the Runs panel on the same screen has already fetched prompt
 * previews for the recent runs, so the two are joined here rather than asking
 * the API for text it deliberately does not store. A run older than that list
 * falls back to its id, which is still enough to grep for.
 */
export function identifyRun(
  run: Pick<AdminUsageRun, "run_id">,
  prompts: Map<string, string>,
): RunIdentity {
  const prompt = (prompts.get(run.run_id) ?? "").trim();
  return {
    title: prompt || `Run ${shortId(run.run_id)}`,
    named: prompt.length > 0,
    runId: run.run_id,
  };
}
