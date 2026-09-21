import { describe, expect, it } from "vitest";

import {
  applyPreset,
  presetLabel,
  type RunPreset,
} from "../components/views/preset-format";

function preset(overrides: Partial<RunPreset> = {}): RunPreset {
  return {
    name: "deep-research",
    label: "Deep research",
    description: "Plans first.",
    effort: "high",
    budget: "high",
    step_plan: true,
    ...overrides,
  };
}

const CURRENT = { effort: "medium", stepPlan: false };

describe("applyPreset", () => {
  it("takes effort and the plan toggle from the preset", () => {
    expect(applyPreset(preset(), CURRENT)).toEqual({
      effort: "high",
      stepPlan: true,
    });
  });

  it("seeds nothing that outlives the turn", () => {
    // THE REGRESSION. A preset used to carry `approval_mode`, both composers
    // passed `approvalMode: ""` as the current value so the preset always won,
    // and both then PUT it onto the conversation — persistent state every
    // later turn reads. Picking "Quick lookup", whose description mentions
    // effort and passages and nothing else, moved a thread out of `plan`
    // (writes denied) into `ask_writes` and left it there. The seed's whole
    // surface is now the two controls the composer shows.
    expect(Object.keys(applyPreset(preset(), CURRENT)).sort()).toEqual([
      "effort",
      "stepPlan",
    ]);
    expect(JSON.stringify(applyPreset(preset(), CURRENT))).not.toContain("approval");
  });

  it("leaves the effort alone when the preset pins the identity value", () => {
    // "" is "whatever you already chose", not a third level — so a user who
    // set high effort and then picked the grounded preset keeps high effort.
    const seeded = applyPreset(
      preset({ name: "grounded-answer", effort: "", step_plan: false }),
      { ...CURRENT, effort: "high" },
    );
    expect(seeded.effort).toBe("high");
    expect(seeded.stepPlan).toBe(false);
  });

  it("returns the current state untouched for auto", () => {
    // Auto is routed server-side from the prompt, so there is nothing to
    // preview; seeding from its placeholder fields would show a policy the
    // turn will not run under.
    const row = preset({ name: "auto", effort: "", step_plan: false });
    expect(applyPreset(row, CURRENT)).toBe(CURRENT);
  });

  it("turns the plan toggle back off when the preset does not use it", () => {
    const seeded = applyPreset(preset({ step_plan: false }), {
      ...CURRENT,
      stepPlan: true,
    });
    expect(seeded.stepPlan).toBe(false);
  });
});

describe("presetLabel", () => {
  it("names every catalogue row", () => {
    expect(presetLabel("")).toBe("No preset");
    expect(presetLabel("auto")).toBe("Auto");
    expect(presetLabel("quick-lookup")).toBe("Quick lookup");
    expect(presetLabel("grounded-answer")).toBe("Grounded answer");
    expect(presetLabel("deep-research")).toBe("Deep research");
    expect(presetLabel("deliverable")).toBe("Deliverable");
  });

  it("passes an unknown name through unchanged", () => {
    // A thread set to a preset this build has since dropped should still say
    // what it is set to. Blanking it would read as "your setting vanished".
    expect(presetLabel("some-future-preset")).toBe("some-future-preset");
  });
});
