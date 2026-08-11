import type { AdminBudget, AdminBudgetCeiling, AdminBudgetSpend } from "@workspace/api-client";
import { formatCount, formatUsd, plural, readSpend, type SpendReading } from "./usage-format";

/**
 * What a run parked on the spend ceiling has to say for itself.
 *
 * ADR 0008 chose to *park* rather than fail: an agent three tool calls into a
 * turn has already created a document and moved a card, so raising on the
 * fourth would destroy the record of the automation to protect the invoice. The
 * cost of that choice is entirely a UI cost — a parked run looks exactly like a
 * run waiting for a person, because it *is* `waiting_for_approval`, and the one
 * field that tells them apart is `paused_reason`.
 *
 * Two rules hold this file together.
 *
 * **A budget park has no `AgentToolCall`.** `_park_for_budget` says so in as
 * many words: the model had not been asked yet, so there is no proposed call
 * and nothing to approve. Anything here that produced approve/deny affordances
 * would be offering a decision the API has no endpoint for.
 *
 * **The spend figure obeys `usage-format`'s three readings.** The window that
 * hit the ceiling can contain calls on models with no configured rate, and the
 * event says how many. A card that prints `$0.00 of $5.00` over ten unpriced
 * calls is lying in the same direction the usage panel already refuses to lie
 * in — so every dollar here goes through `readSpend`, which answers "Not
 * priced" when it has nothing measured to report. There is no second money
 * formatter in this file, and there must not be.
 */

/** `Run.paused_reason` / `WorkflowRun.paused_reason` when the ceiling stopped it. */
export const PAUSED_FOR_BUDGET = "budget";

/** `paused_reason` when a proposed write is waiting on a person instead. */
export const PAUSED_FOR_APPROVAL = "approval";

/** Why the ceiling said stop. Mirrors `services/budget.py`'s three constants. */
export type BudgetReason = "usd" | "tokens" | "unpriced" | "";

/**
 * The `run.waiting_for_budget` payload, validated.
 *
 * The event arrives over the network as `Record<string, unknown>`, so every
 * field is checked rather than cast: a payload from an older API that predates
 * a field must degrade to a card with fewer numbers, never to `NaN of $undefined`.
 */
export type BudgetPark = {
  reason: BudgetReason;
  /** True when the *unattended* ceiling stopped it, not the workspace one. */
  unattended: boolean;
  windowHours: number;
  limitUsd: number | null;
  limitTokens: number | null;
  /** `workspace` when an owner set it, `settings` when the deployment did. */
  limitSource: string;
  spendUsd: number;
  spendTokens: number;
  calls: number;
  unpricedCalls: number;
  /** The API's own sentence. Used only where this module has nothing better. */
  message: string;
};

function num(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function nullableNum(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/**
 * Read a `run.waiting_for_budget` payload, or null if this is not one.
 *
 * Null rather than a defaulted object: a card that renders from nothing is a
 * card that claims a ceiling nobody set. The one field that must be present is
 * `reason` — it is what `budget.exceeds` returns and the whole payload is
 * assembled around it.
 */
export function readBudgetPark(data: Record<string, unknown>): BudgetPark | null {
  const reason = text(data.reason);
  if (!["usd", "tokens", "unpriced"].includes(reason)) return null;
  return {
    reason: reason as BudgetReason,
    unattended: data.unattended === true,
    windowHours: Math.max(1, Math.round(num(data.window_hours, 24))),
    limitUsd: nullableNum(data.limit_usd),
    limitTokens: nullableNum(data.limit_tokens),
    limitSource: text(data.limit_source),
    spendUsd: Math.max(0, num(data.spend_usd)),
    spendTokens: Math.max(0, num(data.spend_tokens)),
    calls: Math.max(0, num(data.calls)),
    unpricedCalls: Math.max(0, num(data.unpriced_calls)),
    message: text(data.message),
  };
}

/**
 * "24 hours" / "7 days" — a window as a value.
 *
 * Named for hours rather than borrowing `usage-format`'s `windowPhrase`: that
 * one counts days because the usage panel's window is chosen in days, and this
 * one is whatever `window_hours` the ceiling was configured with.
 */
export function hoursLabel(hours: number): string {
  // Days only once there is more than one of them. The default window is 24h,
  // and "the last 1 day" is a phrase nobody says out loud.
  if (hours % 24 === 0 && hours >= 48) {
    return `${formatCount(hours / 24)} days`;
  }
  return `${formatCount(hours)} ${hours === 1 ? "hour" : "hours"}`;
}

/** The same window inside a sentence: "in the last 24 hours". */
export function hoursPhrase(hours: number): string {
  return `the last ${hoursLabel(hours)}`;
}

/**
 * A ceiling as a phrase. `null` is *no limit of that kind* and must never
 * render as `$0.00`, which would read as a workspace capped at nothing.
 */
export function ceilingPhrase(
  ceiling: Pick<AdminBudgetCeiling, "usd_per_window" | "tokens_per_window">,
): string {
  const parts: string[] = [];
  if (ceiling.usd_per_window !== null) parts.push(formatUsd(ceiling.usd_per_window));
  if (ceiling.tokens_per_window !== null) {
    parts.push(`${formatCount(ceiling.tokens_per_window)} tokens`);
  }
  return parts.length === 0 ? "No limit" : parts.join(" and ");
}

/**
 * What this window cost, read the way the usage panel reads it.
 *
 * `readSpend` needs to know whether rates exist, and neither the event nor the
 * budget response carries `pricing_configured` for the window in question —
 * but both carry the call counts, and "some call here was priced" is the same
 * fact measured directly. With every call unpriced the answer is "Not priced";
 * with some, a floor marked `+`; with none missing, the exact figure.
 */
export function readBudgetSpend(spend: AdminBudgetSpend): SpendReading {
  return readSpend(spend, spend.unpriced_calls < spend.calls);
}

export type BudgetHoldNote = {
  headline: string;
  /** Why it stopped, in one sentence, naming the numbers that decided it. */
  detail: string;
  /** What has to change for it to continue. Never empty. */
  remedy: string;
  /** Rows for the figures panel: label, value, and how sure the value is. */
  facts: { label: string; value: string; muted?: boolean; title?: string }[];
};

/**
 * The card's whole text, from the event alone.
 *
 * Written to answer the three questions a person stopped mid-turn actually has,
 * in order: *is it broken* (no — it is waiting), *which limit* (this one, over
 * this window), and *what now* (raise it, or wait for the window to roll).
 */
export function describeBudgetPark(park: BudgetPark): BudgetHoldNote {
  const who = park.unattended ? "Unattended workflow spend" : "Workspace spend";
  const window = hoursPhrase(park.windowHours);
  const spend = readBudgetSpend({
    calls: park.calls,
    cost_usd: park.spendUsd,
    total_tokens: park.spendTokens,
    unpriced_calls: park.unpricedCalls,
  });
  const facts: BudgetHoldNote["facts"] = [];

  if (park.reason === "tokens") {
    facts.push({
      label: "Token ceiling",
      value: park.limitTokens === null ? "No limit" : formatCount(park.limitTokens),
    });
    facts.push({ label: "Used", value: formatCount(park.spendTokens) });
  } else {
    facts.push({
      label: "Ceiling",
      value: park.limitUsd === null ? "No limit" : formatUsd(park.limitUsd),
    });
    facts.push({
      label: "Spent",
      value: spend.label,
      muted: spend.kind === "unknown",
      title: spend.detail,
    });
  }
  facts.push({ label: "Window", value: hoursLabel(park.windowHours) });
  facts.push({ label: "Calls", value: formatCount(park.calls) });

  // What the ceiling actually saw. With no calls in the window at all — the
  // state a ceiling of 0 produces, and the one a fresh workspace is in — there
  // is no spend to describe, and "$0.00 spent over 0 calls" describes it twice
  // while implying a measurement nobody took.
  const evidence =
    park.calls === 0
      ? "nothing has been recorded in this window yet"
      : spend.kind === "unknown"
        ? `nothing priced across ${plural(park.calls, "call")}`
        : `${spend.label} spent over ${plural(park.calls, "call")}`;

  if (park.reason === "usd") {
    return {
      headline: park.unattended
        ? "Paused — automations have spent their share"
        : "Paused — this workspace reached its spend limit",
      detail:
        `${who} in ${window} reached the ` +
        `${park.limitUsd === null ? "configured" : formatUsd(park.limitUsd)} ceiling ` +
        `(${evidence}). ` +
        "Nothing was lost: the turn stopped before the next model call and picks " +
        "up exactly where it stopped.",
      remedy:
        "Raise the ceiling and this run continues on its own. Leaving it alone " +
        `also works — the window rolls, and spend older than ${hoursLabel(park.windowHours)} ` +
        "stops counting.",
      facts,
    };
  }
  if (park.reason === "tokens") {
    return {
      headline: park.unattended
        ? "Paused — automations have used their share of tokens"
        : "Paused — this workspace reached its token limit",
      detail:
        `${who} in ${window} reached the ` +
        `${park.limitTokens === null ? "configured" : formatCount(park.limitTokens)}-token ceiling ` +
        `(${
          park.calls === 0
            ? "nothing has been recorded in this window yet"
            : `${formatCount(park.spendTokens)} used over ${plural(park.calls, "call")}`
        }). ` +
        "The turn stopped before the next model call and is resumable.",
      remedy:
        "Raise the token ceiling and this run continues on its own, or wait for " +
        "the window to roll.",
      facts,
    };
  }
  if (park.reason === "unpriced") {
    return {
      headline: "Paused — the dollar ceiling cannot see this spend",
      detail:
        `${who} in ${window} includes ${plural(park.unpricedCalls, "call")} on models ` +
        "with no configured rate, so the dollar ceiling cannot tell whether this " +
        "workspace is over it. Being asked to enforce a limit it cannot measure, " +
        "it stops rather than waving the call through.",
      remedy:
        "Set a token ceiling to bound those calls, or configure MODEL_PRICES on " +
        "the API with a rate for the models below.",
      facts,
    };
  }
  // A reason this build does not know: say the API's own sentence rather than
  // invent one, and still say that it is parked rather than broken.
  return {
    headline: "Paused by the spend limit",
    detail: park.message || `${who} in ${window} reached a configured ceiling.`,
    remedy: "Raise the ceiling to continue, or wait for the window to roll.",
    facts,
  };
}

export type CeilingNote = {
  /** live: a limit is enforced. off: none is set. */
  tone: "live" | "off";
  headline: string;
  detail: string;
};

/**
 * The Admin panel's headline: whether anything is capped at all.
 *
 * "Unset is unlimited, and stays unlimited" is the module's promise to a
 * deployment upgrading into this release, so the day-one state — no row, no
 * numbers, nothing spent — has to read as a deliberate *no ceiling* rather
 * than as a panel that failed to load.
 */
export function describeCeiling(budget: AdminBudget): CeilingNote {
  const { ceiling } = budget;
  if (!budget.enforced) {
    return {
      tone: "off",
      headline: "No spend ceiling — this workspace is unlimited",
      detail:
        "Nothing here stops a runaway loop or a workflow scheduled every " +
        "minute. Set a limit below and runs that reach it park instead of " +
        "spending past it; they resume when you raise it.",
    };
  }
  const source =
    ceiling.source === "workspace"
      ? "Set for this workspace."
      : "Inherited from the deployment's configuration, and can be replaced here.";
  return {
    tone: "live",
    headline: `${ceilingPhrase(ceiling)} per ${hoursLabel(ceiling.window_hours)}`,
    detail:
      `${source} A run that reaches it parks rather than failing, and is ` +
      "released the moment the ceiling is raised past it.",
  };
}

/**
 * The warning that a dollar ceiling standing alone over unpriced calls is not
 * a loose limit but *no* limit — and, worse, one that stops everything.
 *
 * `budget.exceeds` returns `unpriced` in exactly this shape, so a workspace
 * configured this way is one call away from parking every run it has. Saying so
 * where the ceiling is edited is the difference between a surprising outage and
 * a decision.
 */
export function unpricedRisk(
  ceiling: Pick<AdminBudgetCeiling, "usd_per_window" | "tokens_per_window">,
  spend: Pick<AdminBudgetSpend, "unpriced_calls">,
): string {
  if (ceiling.usd_per_window === null || ceiling.tokens_per_window !== null) return "";
  if (spend.unpriced_calls <= 0) return "";
  return (
    `${plural(spend.unpriced_calls, "call")} in this window ran on a model with no ` +
    "configured rate, so the dollar ceiling cannot see them. Until MODEL_PRICES " +
    "covers those models — or a token ceiling bounds them — every run here parks."
  );
}
