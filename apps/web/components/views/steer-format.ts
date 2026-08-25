import type { AgentToolCall } from "@workspace/api-client";

/**
 * Whether the mid-turn steering strip should render.
 *
 * The server refuses a steer for any run outside {queued, running} with a
 * 409, so the strip must hide for every parked shape — and the parked shapes
 * do not share one signal:
 *
 * - an approval park has a `proposed` call in `agentCalls` … unless the
 *   surface filtered it out (the subject panel hides calls it is deciding
 *   inline), which is why `runStatus` is consulted too — `followRun` sets it
 *   to "Waiting for your approval" from the event stream, which no filter
 *   touches;
 * - a budget park writes NO AgentToolCall at all — `budgetPark` is its only
 *   client-side trace.
 *
 * Pure and separately tested because the rule is a conjunction of four
 * signals from three sources, and an inline JSX condition of that shape is
 * exactly where the next surface forgets one.
 */
export function steerStripVisible(args: {
  activeRun: string | null;
  hasSteer: boolean;
  budgetPark: unknown;
  runStatus: string;
  agentCalls: Pick<AgentToolCall, "run_id" | "status">[];
}): boolean {
  const { activeRun, hasSteer, budgetPark, runStatus, agentCalls } = args;
  if (!activeRun || !hasSteer || budgetPark) return false;
  if (runStatus.toLowerCase().includes("waiting")) return false;
  return !agentCalls.some(
    (call) => call.run_id === activeRun && call.status === "proposed",
  );
}
