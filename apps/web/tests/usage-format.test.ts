import { describe, expect, it } from "vitest";
import type { AdminUsage, AdminUsageGroup } from "@workspace/api-client";
import {
  agentLabel,
  barMetric,
  barShare,
  describePricing,
  formatCount,
  formatUsd,
  identifyRun,
  plural,
  readSpend,
  shortId,
  windowLabel,
  windowPhrase,
} from "../components/views/usage-format";

/**
 * The panel's whole claim is that it does not invent money. MODEL_PRICES ships
 * empty, so the default deployment has real tokens and no cost, and every
 * assertion below about "$0.00" is guarding the one bug that would make this
 * screen worse than not having it.
 */

function group(over: Partial<AdminUsageGroup> = {}): AdminUsageGroup {
  return {
    key: "gpt-5.5",
    label: "gpt-5.5",
    calls: 10,
    input_tokens: 8_000,
    output_tokens: 2_000,
    total_tokens: 10_000,
    cost_usd: 0,
    unpriced_calls: 0,
    ...over,
  };
}

function usage(over: Partial<AdminUsage> = {}): AdminUsage {
  const totals = {
    calls: 0,
    input_tokens: 0,
    cached_input_tokens: 0,
    output_tokens: 0,
    reasoning_tokens: 0,
    total_tokens: 0,
    cost_usd: 0,
    priced_calls: 0,
    unpriced_calls: 0,
    ...(over.totals ?? {}),
  };
  return {
    window_days: 30,
    since: "2026-01-01T00:00:00Z",
    by_model: [],
    by_user: [],
    by_operation: [],
    by_agent: [],
    top_runs: [],
    unpriced_models: [],
    pricing_configured: false,
    ...over,
    totals,
  };
}

describe("formatCount", () => {
  it("groups thousands", () => {
    expect(formatCount(0)).toBe("0");
    expect(formatCount(999)).toBe("999");
    expect(formatCount(1_000)).toBe("1,000");
    expect(formatCount(1_234_567)).toBe("1,234,567");
  });

  it("survives a number the API could not have sent", () => {
    expect(formatCount(Number.NaN)).toBe("0");
    expect(formatCount(12.6)).toBe("13");
  });
});

describe("formatUsd", () => {
  it("keeps sub-cent spend visible instead of rounding it to nothing", () => {
    // Two cents' worth of embeddings is a real figure; $0.00 is not.
    expect(formatUsd(0.0042)).toBe("$0.0042");
    expect(formatUsd(0.00004)).toBe("<$0.0001");
  });

  it("uses cents in the middle and drops them once they stop mattering", () => {
    expect(formatUsd(0.01)).toBe("$0.01");
    expect(formatUsd(12.3456)).toBe("$12.35");
    expect(formatUsd(999.994)).toBe("$999.99");
    expect(formatUsd(1_234.56)).toBe("$1,235");
  });
});

describe("plural", () => {
  it("agrees with its noun and separates its thousands", () => {
    expect(plural(1, "call")).toBe("1 call");
    expect(plural(0, "call")).toBe("0 calls");
    expect(plural(2_500, "call")).toBe("2,500 calls");
  });
});

describe("readSpend", () => {
  it("refuses to print a number when nothing is priced", () => {
    const reading = readSpend(group({ calls: 40, unpriced_calls: 40 }), false);
    expect(reading.kind).toBe("unknown");
    expect(reading.label).toBe("Not priced");
    expect(reading.label).not.toContain("$");
    expect(reading.detail).toMatch(/no model rates are configured/i);
  });

  it("still refuses when rates exist but this slice's model has none", () => {
    const reading = readSpend(group({ calls: 3, unpriced_calls: 3 }), true);
    expect(reading.kind).toBe("unknown");
    expect(reading.detail).toMatch(/cost is unknown/i);
  });

  it("marks a partly-priced slice as a floor", () => {
    const reading = readSpend(
      group({ calls: 10, unpriced_calls: 4, cost_usd: 1.5 }),
      true,
    );
    expect(reading.kind).toBe("floor");
    expect(reading.label).toBe("$1.50+");
    expect(reading.detail).toMatch(/at least this much: 6 calls priced, 4 on models/i);
  });

  it("prints the plain figure when every call had a rate", () => {
    const reading = readSpend(group({ calls: 10, cost_usd: 0.004 }), true);
    expect(reading).toMatchObject({ kind: "exact", label: "$0.0040" });
  });

  it("never reports a zero it did not measure", () => {
    // The shipped default: real tokens, cost summed to 0.0 because nothing is
    // priced. This is the exact input that used to render as $0.00.
    const reading = readSpend(group({ calls: 10, unpriced_calls: 10, cost_usd: 0 }), false);
    expect(reading.label).not.toBe("$0.00");
  });
});

describe("describePricing", () => {
  it("leads with the missing configuration when there are no rates", () => {
    const notice = describePricing(
      usage({
        unpriced_models: ["gpt-5.5", "text-embedding-3-small"],
        totals: { calls: 12, unpriced_calls: 12 } as AdminUsage["totals"],
      }),
    );
    expect(notice.tone).toBe("off");
    expect(notice.headline).toMatch(/unknown, not zero/);
    expect(notice.detail).toMatch(/MODEL_PRICES/);
    expect(notice.models).toEqual(["gpt-5.5", "text-embedding-3-small"]);
  });

  it("says so on day one, when there are no rates and no calls either", () => {
    const notice = describePricing(usage());
    expect(notice.tone).toBe("off");
    expect(notice.detail).toMatch(/spend will appear here/i);
  });

  it("calls a partly-priced window a floor and names what is missing", () => {
    const notice = describePricing(
      usage({
        pricing_configured: true,
        unpriced_models: ["some-local-model"],
        totals: {
          calls: 10,
          priced_calls: 7,
          unpriced_calls: 3,
        } as AdminUsage["totals"],
      }),
    );
    expect(notice.tone).toBe("gaps");
    expect(notice.headline).toMatch(/^3 calls ran on a model with no configured rate/);
    expect(notice.detail).toMatch(/floor, not the bill/);
    expect(notice.models).toEqual(["some-local-model"]);
  });

  it("is quiet once everything is priced", () => {
    const notice = describePricing(
      usage({
        pricing_configured: true,
        totals: {
          calls: 4,
          priced_calls: 4,
          total_tokens: 12_000,
        } as AdminUsage["totals"],
      }),
    );
    expect(notice.tone).toBe("clear");
    expect(notice.detail).toContain("12,000");
    expect(notice.models).toEqual([]);
  });

  it("reports an empty priced window as empty rather than as a warning", () => {
    const notice = describePricing(usage({ pricing_configured: true, window_days: 7 }));
    expect(notice.tone).toBe("clear");
    expect(notice.headline).toMatch(/no model call was recorded in the last 7 days/i);
  });
});

describe("barMetric", () => {
  it("measures tokens whenever any cost is missing", () => {
    expect(barMetric(usage({ totals: { calls: 5 } as AdminUsage["totals"] }))).toBe("tokens");
    expect(
      barMetric(
        usage({
          pricing_configured: true,
          totals: { calls: 5, unpriced_calls: 1, cost_usd: 2 } as AdminUsage["totals"],
        }),
      ),
    ).toBe("tokens");
  });

  it("measures cost only when the whole window is priced", () => {
    expect(
      barMetric(
        usage({
          pricing_configured: true,
          totals: { calls: 5, priced_calls: 5, cost_usd: 2 } as AdminUsage["totals"],
        }),
      ),
    ).toBe("cost");
  });
});

describe("barShare", () => {
  it("is a percentage of the biggest row", () => {
    expect(barShare(50, 100)).toBe(50);
    expect(barShare(100, 100)).toBe(100);
  });

  it("keeps a tiny but real row visible, and draws nothing for a zero", () => {
    expect(barShare(0.0001, 100)).toBe(2);
    expect(barShare(0, 100)).toBe(0);
    expect(barShare(5, 0)).toBe(0);
  });
});

describe("identifyRun", () => {
  const prompts = new Map([["run-a1b2", "Summarise every incident report from March"]]);

  it("names a run by the prompt the Runs panel already fetched", () => {
    const identity = identifyRun({ run_id: "run-a1b2" }, prompts);
    expect(identity).toEqual({
      title: "Summarise every incident report from March",
      named: true,
      runId: "run-a1b2",
    });
  });

  it("falls back to something greppable for a run older than that list", () => {
    const identity = identifyRun({ run_id: "9f3c7d21-0000-4a11-bd0e-77" }, prompts);
    expect(identity.named).toBe(false);
    expect(identity.title).toBe("Run 9f3c7d21");
    expect(identity.runId).toBe("9f3c7d21-0000-4a11-bd0e-77");
  });
});

describe("window labels", () => {
  it("names each window the way a person would", () => {
    expect(windowLabel(1)).toBe("Last 24 hours");
    expect(windowLabel(7)).toBe("Last 7 days");
    expect(windowLabel(30)).toBe("Last 30 days");
    expect(windowLabel(90)).toBe("Last 3 months");
    expect(windowLabel(365)).toBe("Last year");
  });

  it("lower-cases the label for use mid-sentence", () => {
    expect(windowPhrase(30)).toBe("the last 30 days");
  });
});

describe("shortId", () => {
  it("takes the first uuid segment", () => {
    expect(shortId("9f3c7d21-0000-4a11")).toBe("9f3c7d21");
  });

  it("keeps something identifiable out of an id that is not a uuid", () => {
    // "run-runaway-0001".split("-")[0] is "run", which identifies nothing.
    expect(shortId("run-runaway-0001")).toBe("run-runaway");
    expect(shortId("plain")).toBe("plain");
    expect(shortId("")).toBe("");
  });
});

describe("agentLabel", () => {
  it("wears a live agent's name as-is", () => {
    expect(agentLabel({ key: "9f3c7d21-0000-4a11", label: "Scribe" })).toBe("Scribe");
  });

  it("dresses a deleted agent's id — the API echoes the key as the label", () => {
    // A row whose spend outlived its agent arrives with label === key; a bare
    // uuid is a poor row title, so it reads as "Agent <short id>".
    expect(
      agentLabel({ key: "9f3c7d21-0000-4a11", label: "9f3c7d21-0000-4a11" }),
    ).toBe("Agent 9f3c7d21");
    expect(agentLabel({ key: "9f3c7d21-0000-4a11", label: "" })).toBe(
      "Agent 9f3c7d21",
    );
  });

  it("names the no-agent bucket in words", () => {
    // Embeddings, ingest and compiles bill no agent; an empty row head would
    // read as a rendering bug rather than a category.
    expect(agentLabel({ key: "", label: "" })).toBe("Background work");
  });
});
