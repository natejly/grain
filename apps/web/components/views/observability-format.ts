/**
 * The pure arithmetic and wording behind the Observability panel, kept out of
 * the JSX so the rules that are easy to get subtly wrong — a `null` percentile
 * that must never print as `0ms`, a window said in the unit a person would use,
 * a bar that stays visible for a real-but-tiny value — can be asserted directly
 * instead of eyeballed in a browser. A sibling of `usage-format` and
 * `budget-format`, and tested the same way.
 */

/** The windows the panel offers, in hours. The API accepts 1–720. */
export const OBS_WINDOWS = [1, 6, 24, 72, 168, 720] as const;

/** The percentile rows a latency metric renders, in worsening order. */
export const PERCENTILES = [
  { key: "p50_ms", label: "p50" },
  { key: "p90_ms", label: "p90" },
  { key: "p99_ms", label: "p99" },
  { key: "max_ms", label: "max" },
] as const;

/** A window's hours as the phrase a person would use for it. */
export function obsWindowLabel(hours: number): string {
  if (hours === 1) return "Last hour";
  if (hours < 24) return `Last ${hours} hours`;
  const days = hours / 24;
  if (days === 1) return "Last 24 hours";
  if (days === 7) return "Last 7 days";
  if (days === 30) return "Last 30 days";
  return `Last ${days} days`;
}

/**
 * Milliseconds as the coarsest unit that still reads at a glance. `null` is a
 * measured absence, not zero, so it prints an em dash — the whole reason the
 * backend keeps every percentile `Optional`.
 */
export function formatMs(ms: number | null): string {
  if (ms === null) return "—";
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)} s`;
  return `${(ms / 60_000).toFixed(1)} min`;
}

/** A whole-seconds age as the coarsest unit that still reads at a glance. */
export function formatAge(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86_400)}d`;
}

/**
 * An error rate as a percentage, keeping a real-but-sub-one-percent rate
 * visible with a decimal rather than rounding it away to `0%`.
 */
export function formatErrorRate(rate: number): string {
  return `${(rate * 100).toFixed(rate > 0 && rate < 0.01 ? 1 : 0)}%`;
}

/** Share of a value against a max, clamped to a visible floor when non-zero. */
export function share(value: number, max: number): number {
  if (max <= 0 || value <= 0) return 0;
  return Math.max(2, Math.round((value / max) * 100));
}
