/**
 * The live plan timeline, as a pure reducer.
 *
 * A plan-then-execute turn narrates itself through two run events —
 * `plan.submitted` and `plan.step` — and the composer renders them as they
 * arrive. Folding them here rather than in the stream handler keeps the
 * handler a transport and makes the interesting behaviour testable with no
 * DOM: what a malformed payload does, what an out-of-range step does, and what
 * every unrelated event does.
 *
 * The identity rule is what lets the caller stay simple. `foldPlanEvent`
 * returns its input unchanged for anything it does not recognise, so the
 * stream can pipe EVERY event through it without first asking which ones
 * matter.
 */

export type PlanStep = {
  index: number;
  question: string;
  queries: string[];
  summary: string;
  done: boolean;
};

export type RunPlan = { steps: PlanStep[] } | null;

export type PlanEvent = {
  event: string;
  data: Record<string, unknown>;
};

const PLAN_SUBMITTED = "plan.submitted";
const PLAN_STEP = "plan.step";

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function asQueries(value: unknown): string[] {
  return Array.isArray(value) ? value.map(asString).filter(Boolean) : [];
}

/**
 * The plan after one event, or the same plan when the event says nothing
 * about it.
 *
 * A `plan.step` naming an index the plan does not have is IGNORED rather than
 * appended: a malformed payload must not be able to invent a step the model
 * never planned, because the timeline is the user's only view of what the turn
 * committed to doing.
 */
export function foldPlanEvent(plan: RunPlan, event: PlanEvent): RunPlan {
  if (event.event === PLAN_SUBMITTED) {
    const raw = event.data?.steps;
    if (!Array.isArray(raw)) return plan;
    const steps = raw.map((item, position) => {
      const step = (item ?? {}) as Record<string, unknown>;
      const index = typeof step.index === "number" ? step.index : position + 1;
      return {
        index,
        question: asString(step.question),
        queries: asQueries(step.queries),
        summary: "",
        done: false,
      };
    });
    return steps.length ? { steps } : plan;
  }
  if (event.event === PLAN_STEP) {
    if (!plan) return plan;
    const index = event.data?.index;
    if (typeof index !== "number") return plan;
    if (!plan.steps.some((step) => step.index === index)) return plan;
    return {
      steps: plan.steps.map((step) =>
        step.index === index
          ? { ...step, done: true, summary: asString(event.data?.summary) }
          : step,
      ),
    };
  }
  return plan;
}

/** "Step 2 of 4: <question>" for the first unfinished step; "" when there is
 *  no plan, and "" once every step is done — the answer is the status then. */
export function planStatusLine(plan: RunPlan): string {
  if (!plan || !plan.steps.length) return "";
  const next = plan.steps.find((step) => !step.done);
  if (!next) return "";
  const position = plan.steps.indexOf(next) + 1;
  const question = next.question ? `: ${next.question}` : "";
  return `Step ${position} of ${plan.steps.length}${question}`;
}
