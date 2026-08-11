import { describe, expect, it } from "vitest";
import type { AdminBudget } from "@workspace/api-client";
import {
  ceilingPhrase,
  describeBudgetPark,
  describeCeiling,
  hoursLabel,
  hoursPhrase,
  readBudgetPark,
  readBudgetSpend,
  unpricedRisk,
  type BudgetPark,
} from "../components/views/budget-format";
import {
  isBudgetPark,
  nodeStatusLabel,
  runStatusLabel,
} from "../components/views/workflow-format";

/**
 * The rules that keep a budget park from reading as an approval, and a budget
 * figure from claiming a precision the deployment cannot back up.
 *
 * Both failures are silent in a browser — a wrong label looks like a label, and
 * `$0.00` looks like a measurement — so they are asserted here rather than
 * left to a screenshot.
 */

/** The payload `_park_for_budget` writes, field for field. */
function payload(extra: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    status: "waiting_for_approval",
    paused_reason: "budget",
    message: "Workspace spend reached the $5.00 ceiling for the last 24h.",
    reason: "usd",
    unattended: false,
    window_hours: 24,
    limit_usd: 5,
    limit_tokens: null,
    limit_source: "workspace",
    spend_usd: 5.25,
    spend_tokens: 412_000,
    calls: 84,
    unpriced_calls: 0,
    ...extra,
  };
}

function park(extra: Partial<BudgetPark> = {}): BudgetPark {
  return { ...readBudgetPark(payload())!, ...extra };
}

function budget(extra: Partial<AdminBudget> = {}): AdminBudget {
  return {
    ceiling: {
      window_hours: 24,
      usd_per_window: null,
      tokens_per_window: null,
      source: "settings",
    },
    unattended_ceiling: {
      window_hours: 24,
      usd_per_window: null,
      tokens_per_window: null,
      source: "settings",
    },
    spend: { calls: 0, cost_usd: 0, total_tokens: 0, unpriced_calls: 0 },
    unattended_spend: { calls: 0, cost_usd: 0, total_tokens: 0, unpriced_calls: 0 },
    enforced: false,
    pricing_configured: false,
    runs_parked_on_budget: [],
    resumed_run_ids: [],
    ...extra,
  };
}

describe("readBudgetPark", () => {
  it("reads the event the API actually writes", () => {
    const read = readBudgetPark(payload());
    expect(read).not.toBeNull();
    expect(read!.reason).toBe("usd");
    expect(read!.limitUsd).toBe(5);
    expect(read!.limitTokens).toBeNull();
    expect(read!.spendTokens).toBe(412_000);
    expect(read!.unattended).toBe(false);
  });

  it("refuses a payload that is not a budget park", () => {
    // Every other run event goes through the same stream; only this one has a
    // reason `budget.exceeds` could have returned.
    expect(readBudgetPark({ status: "waiting_for_approval" })).toBeNull();
    expect(readBudgetPark({ reason: "" })).toBeNull();
    expect(readBudgetPark({ reason: "vibes" })).toBeNull();
  });

  it("survives a payload missing the fields a newer API would add", () => {
    const read = readBudgetPark({ reason: "tokens" });
    expect(read).not.toBeNull();
    // Absent is null, never NaN and never a fabricated zero limit.
    expect(read!.limitUsd).toBeNull();
    expect(read!.limitTokens).toBeNull();
    expect(read!.spendUsd).toBe(0);
    expect(read!.windowHours).toBe(24);
  });

  it("keeps a null limit null rather than reading it as zero", () => {
    const read = readBudgetPark(payload({ limit_usd: null, reason: "tokens" }));
    expect(read!.limitUsd).toBeNull();
  });

  it("marks an unattended park as one", () => {
    expect(readBudgetPark(payload({ unattended: true }))!.unattended).toBe(true);
  });
});

describe("describeBudgetPark", () => {
  it("says it is waiting, not broken, and names the ceiling and the spend", () => {
    const note = describeBudgetPark(park());
    expect(note.headline).toMatch(/Paused/);
    expect(note.detail).toContain("$5.00");
    expect(note.detail).toContain("$5.25");
    expect(note.detail).toMatch(/picks up exactly where it stopped/);
    expect(note.remedy).toMatch(/Raise the ceiling/);
    expect(note.facts.map((fact) => fact.label)).toContain("Ceiling");
  });

  it("tells an unattended park from a workspace one", () => {
    expect(describeBudgetPark(park({ unattended: true })).headline).toMatch(
      /automations/i,
    );
    expect(describeBudgetPark(park()).headline).toMatch(/workspace/i);
  });

  it("refuses to price a window in which nothing was priced", () => {
    // The shipped default: MODEL_PRICES is empty, so cost_usd is zero by
    // arithmetic. "$0.00 of $5.00" would be a measurement nobody took.
    const note = describeBudgetPark(
      park({ spendUsd: 0, calls: 84, unpricedCalls: 84 }),
    );
    const spent = note.facts.find((fact) => fact.label === "Spent");
    expect(spent?.value).toBe("Not priced");
    expect(spent?.muted).toBe(true);
    expect(note.detail).not.toContain("$0.00");
  });

  it("describes an empty window as empty rather than as $0.00 over 0 calls", () => {
    // What a ceiling of 0 produces, and what a fresh workspace is in. Saying
    // "$0.00 spent over 0 calls" describes nothing twice while implying a
    // measurement nobody took.
    const note = describeBudgetPark(park({ spendUsd: 0, spendTokens: 0, calls: 0 }));
    expect(note.detail).toContain("nothing has been recorded in this window yet");
    expect(note.detail).not.toContain("0 calls");
  });

  it("marks a partly priced window as a floor", () => {
    const note = describeBudgetPark(park({ spendUsd: 4.5, calls: 84, unpricedCalls: 9 }));
    expect(note.facts.find((fact) => fact.label === "Spent")?.value).toBe("$4.50+");
  });

  it("counts tokens rather than money when the token ceiling stopped it", () => {
    const note = describeBudgetPark(
      park({ reason: "tokens", limitUsd: null, limitTokens: 400_000 }),
    );
    expect(note.facts.map((fact) => fact.label)).toContain("Token ceiling");
    expect(note.detail).toContain("400,000-token");
    expect(note.detail).not.toContain("$");
  });

  it("explains the unpriced stop as blindness, not overspend", () => {
    const note = describeBudgetPark(park({ reason: "unpriced", unpricedCalls: 7 }));
    expect(note.detail).toMatch(/cannot tell/);
    expect(note.remedy).toContain("MODEL_PRICES");
  });

  it("falls back to the API's own sentence for a reason it does not know", () => {
    const unknown = { ...park(), reason: "" as const, message: "Something new stopped it." };
    const note = describeBudgetPark(unknown);
    expect(note.detail).toBe("Something new stopped it.");
    expect(note.headline).toMatch(/Paused/);
  });
});

describe("ceilings and windows", () => {
  it("renders no limit as words, never as $0.00", () => {
    expect(ceilingPhrase({ usd_per_window: null, tokens_per_window: null })).toBe(
      "No limit",
    );
    expect(ceilingPhrase({ usd_per_window: 0, tokens_per_window: null })).toBe("$0.00");
    expect(ceilingPhrase({ usd_per_window: 5, tokens_per_window: 400_000 })).toBe(
      "$5.00 and 400,000 tokens",
    );
  });

  it("says a window in whichever unit reads", () => {
    // Days only once there is more than one: "the last 1 day" is a phrase
    // nobody says, and 24h is the default window.
    expect(hoursLabel(24)).toBe("24 hours");
    expect(hoursLabel(48)).toBe("2 days");
    expect(hoursLabel(168)).toBe("7 days");
    expect(hoursLabel(6)).toBe("6 hours");
    expect(hoursLabel(1)).toBe("1 hour");
    expect(hoursPhrase(24)).toBe("the last 24 hours");
  });
});

describe("readBudgetSpend", () => {
  it("reads an all-unpriced window as unknown", () => {
    const spend = readBudgetSpend({
      calls: 12,
      cost_usd: 0,
      total_tokens: 900,
      unpriced_calls: 12,
    });
    expect(spend.kind).toBe("unknown");
    expect(spend.label).toBe("Not priced");
  });

  it("reads a fully priced window exactly", () => {
    const spend = readBudgetSpend({
      calls: 12,
      cost_usd: 1.5,
      total_tokens: 900,
      unpriced_calls: 0,
    });
    expect(spend.kind).toBe("exact");
    expect(spend.label).toBe("$1.50");
  });
});

describe("describeCeiling", () => {
  it("reads day one — no ceiling, no usage — as a deliberate choice", () => {
    const note = describeCeiling(budget());
    expect(note.tone).toBe("off");
    expect(note.headline).toMatch(/No spend ceiling/);
    expect(note.detail).not.toContain("$");
  });

  it("names the limit and where it came from once one exists", () => {
    const note = describeCeiling(
      budget({
        enforced: true,
        ceiling: {
          window_hours: 24,
          usd_per_window: 20,
          tokens_per_window: null,
          source: "workspace",
        },
      }),
    );
    expect(note.tone).toBe("live");
    expect(note.headline).toBe("$20.00 per 24 hours");
    expect(note.detail).toMatch(/Set for this workspace/);
  });

  it("says a settings ceiling can be replaced here", () => {
    const note = describeCeiling(
      budget({
        enforced: true,
        ceiling: {
          window_hours: 12,
          usd_per_window: null,
          tokens_per_window: 1_000_000,
          source: "settings",
        },
      }),
    );
    expect(note.headline).toBe("1,000,000 tokens per 12 hours");
    expect(note.detail).toMatch(/Inherited from the deployment/);
  });
});

describe("unpricedRisk", () => {
  it("warns when a dollar ceiling stands alone over unpriced calls", () => {
    // `budget.exceeds` returns `unpriced` here, which stops *everything* — so
    // this configuration is one call away from parking every run.
    expect(
      unpricedRisk(
        { usd_per_window: 5, tokens_per_window: null },
        { unpriced_calls: 12 },
      ),
    ).toMatch(/every run here parks/);
  });

  it("stays quiet when a token ceiling bounds them", () => {
    expect(
      unpricedRisk(
        { usd_per_window: 5, tokens_per_window: 400_000 },
        { unpriced_calls: 12 },
      ),
    ).toBe("");
  });

  it("stays quiet when every call is priced, or nothing is capped in dollars", () => {
    expect(
      unpricedRisk({ usd_per_window: 5, tokens_per_window: null }, { unpriced_calls: 0 }),
    ).toBe("");
    expect(
      unpricedRisk(
        { usd_per_window: null, tokens_per_window: null },
        { unpriced_calls: 12 },
      ),
    ).toBe("");
  });
});

describe("a budget park is not an approval", () => {
  it("labels a budget-parked run distinctly from one awaiting a decision", () => {
    expect(runStatusLabel("waiting_for_approval", "budget")).toBe(
      "Held by the spend limit",
    );
    expect(runStatusLabel("waiting_for_approval", "approval")).toBe(
      "Waiting for approval",
    );
    // An older API that sends no reason at all must not be relabelled: the
    // approval park is the one that predates the column.
    expect(runStatusLabel("waiting_for_approval")).toBe("Waiting for approval");
  });

  it("labels the node the same way, since the node row carries no reason", () => {
    expect(nodeStatusLabel("waiting_for_approval", "budget")).toBe(
      "Held by the spend limit",
    );
    expect(nodeStatusLabel("waiting_for_approval")).toBe("Parked for approval");
  });

  it("only calls it a budget park when the run is actually parked", () => {
    expect(isBudgetPark("waiting_for_approval", "budget")).toBe(true);
    expect(isBudgetPark("running", "budget")).toBe(false);
    expect(isBudgetPark("succeeded", "")).toBe(false);
  });

  it("leaves every other status alone", () => {
    expect(runStatusLabel("succeeded", "budget")).toBe("Succeeded");
    expect(runStatusLabel("failed", "")).toBe("Failed");
  });
});
