import { describe, expect, it } from "vitest";
import { steerStripVisible } from "../components/views/steer-format";

const base = {
  activeRun: "run-1",
  hasSteer: true,
  budgetPark: null as unknown,
  runStatus: "running",
  agentCalls: [] as { run_id: string; status: string }[],
};

describe("steerStripVisible", () => {
  it("shows for a plain streaming run", () => {
    expect(steerStripVisible(base)).toBe(true);
  });

  it("hides when there is no active run or no steer handler", () => {
    expect(steerStripVisible({ ...base, activeRun: null })).toBe(false);
    expect(steerStripVisible({ ...base, hasSteer: false })).toBe(false);
  });

  it("hides on a budget park, which writes no AgentToolCall at all", () => {
    expect(
      steerStripVisible({ ...base, budgetPark: { reason: "usd" } }),
    ).toBe(false);
  });

  it("hides while a call of this run is proposed", () => {
    expect(
      steerStripVisible({
        ...base,
        agentCalls: [{ run_id: "run-1", status: "proposed" }],
      }),
    ).toBe(false);
  });

  it("ignores another run's proposed call", () => {
    expect(
      steerStripVisible({
        ...base,
        agentCalls: [{ run_id: "run-9", status: "proposed" }],
      }),
    ).toBe(true);
  });

  it("hides on a waiting runStatus even when the proposed call was filtered out", () => {
    // The subject panel hides calls it is deciding inline, so the list can be
    // empty while the run is parked — the streamed status cannot be filtered.
    expect(
      steerStripVisible({ ...base, runStatus: "Waiting for your approval" }),
    ).toBe(false);
  });

  it("treats settled statuses as steerable only while a run is active", () => {
    // followRun clears activeRun at settle; a stale status string alone must
    // not resurrect the strip.
    expect(
      steerStripVisible({ ...base, activeRun: null, runStatus: "running" }),
    ).toBe(false);
  });
});
