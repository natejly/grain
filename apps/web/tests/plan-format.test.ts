import { describe, expect, it } from "vitest";

import {
  foldPlanEvent,
  planStatusLine,
  type RunPlan,
} from "../components/views/plan-format";

const SUBMITTED = {
  event: "plan.submitted",
  data: {
    steps: [
      { index: 1, question: "What changed?", queries: ["migration diff"] },
      { index: 2, question: "When did it slip?", queries: ["timeline", "dates"] },
    ],
  },
};

function submitted(): RunPlan {
  return foldPlanEvent(null, SUBMITTED);
}

describe("foldPlanEvent", () => {
  it("builds the timeline from a submitted plan", () => {
    const plan = submitted();
    expect(plan?.steps).toHaveLength(2);
    expect(plan?.steps[0]).toEqual({
      index: 1,
      question: "What changed?",
      queries: ["migration diff"],
      summary: "",
      done: false,
    });
  });

  it("marks the matching step done and stores its summary", () => {
    const plan = foldPlanEvent(submitted(), {
      event: "plan.step",
      data: { index: 1, summary: "The index moved." },
    });
    expect(plan?.steps[0].done).toBe(true);
    expect(plan?.steps[0].summary).toBe("The index moved.");
    expect(plan?.steps[1].done).toBe(false);
  });

  it("ignores a step for an index the plan does not have", () => {
    // A malformed payload must not invent a step the model never planned: the
    // timeline is the user's only view of what the turn committed to doing.
    const plan = submitted();
    const after = foldPlanEvent(plan, {
      event: "plan.step",
      data: { index: 7, summary: "From nowhere." },
    });
    expect(after?.steps).toHaveLength(2);
    expect(after?.steps.every((step) => !step.done)).toBe(true);
  });

  it("is the identity for every unrelated event", () => {
    // Which is what lets the stream pipe EVERY event through it rather than
    // first asking which ones matter.
    const plan = submitted();
    for (const event of ["message.delta", "tool.started", "run.completed"]) {
      expect(foldPlanEvent(plan, { event, data: { index: 1 } })).toBe(plan);
    }
    expect(foldPlanEvent(null, { event: "message.delta", data: {} })).toBeNull();
  });

  it("ignores a malformed submission rather than clearing the plan", () => {
    expect(foldPlanEvent(null, { event: "plan.submitted", data: {} })).toBeNull();
    expect(
      foldPlanEvent(null, { event: "plan.submitted", data: { steps: [] } }),
    ).toBeNull();
    const plan = submitted();
    expect(
      foldPlanEvent(plan, { event: "plan.submitted", data: { steps: "nope" } }),
    ).toBe(plan);
  });

  it("ignores a step event before any plan arrived", () => {
    expect(
      foldPlanEvent(null, { event: "plan.step", data: { index: 1, summary: "x" } }),
    ).toBeNull();
  });
});

describe("planStatusLine", () => {
  it("names the first unfinished step", () => {
    expect(planStatusLine(submitted())).toBe("Step 1 of 2: What changed?");
    const midway = foldPlanEvent(submitted(), {
      event: "plan.step",
      data: { index: 1, summary: "Done." },
    });
    expect(planStatusLine(midway)).toBe("Step 2 of 2: When did it slip?");
  });

  it("is empty with no plan and once every step is done", () => {
    expect(planStatusLine(null)).toBe("");
    let plan = submitted();
    plan = foldPlanEvent(plan, { event: "plan.step", data: { index: 1, summary: "a" } });
    plan = foldPlanEvent(plan, { event: "plan.step", data: { index: 2, summary: "b" } });
    // The answer is the status at that point, not a step.
    expect(planStatusLine(plan)).toBe("");
  });
});
