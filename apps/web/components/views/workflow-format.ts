import type {
  Workflow,
  WorkflowCompileFinding,
  WorkflowGraph,
  WorkflowNodeRunStatus,
  WorkflowRunStatus,
} from "@workspace/api-client";
import { PAUSED_FOR_APPROVAL, PAUSED_FOR_BUDGET } from "./budget-format";

/**
 * Everything the workflow surface has to *say*, kept out of the components that
 * say it — layering a DAG, naming a state, and describing a schedule without
 * promising more than the deployment can keep.
 *
 * The last one is the reason this file is separate and tested. ADR 0007 is
 * explicit that a UI must not describe a schedule as active when nothing can
 * fire it, and "nothing can fire it" is four conditions deep (is it a schedule,
 * is the workflow active, is a ticker configured, has it ever dispatched). That
 * belongs somewhere a test can hold it still.
 */

/**
 * The graph as rows, top to bottom: every node sits one row below its deepest
 * dependency. Kahn's algorithm, the same order the executor walks in.
 *
 * A cycle cannot reach here — the compiler rejects one — but a stored graph is
 * data from the network, so anything left unplaced when the queue drains is
 * appended as a final row rather than dropped or looped over forever. A node
 * that is invisible is worse than a node in the wrong row.
 */
export function layerGraph(graph: WorkflowGraph): string[][] {
  const ids = graph.nodes.map((node) => node.id);
  const known = new Set(ids);
  const depth = new Map<string, number>(ids.map((id) => [id, 0]));
  const indegree = new Map<string, number>(ids.map((id) => [id, 0]));
  const downstream = new Map<string, string[]>(ids.map((id) => [id, []]));

  for (const edge of graph.edges) {
    // An edge naming a node the graph does not declare is not renderable, and
    // counting it would strand its target at indegree 1 forever.
    if (!known.has(edge.from) || !known.has(edge.to) || edge.from === edge.to) continue;
    downstream.get(edge.from)!.push(edge.to);
    indegree.set(edge.to, indegree.get(edge.to)! + 1);
  }

  const queue = ids.filter((id) => indegree.get(id) === 0);
  const placed = new Set<string>(queue);
  for (let cursor = 0; cursor < queue.length; cursor += 1) {
    const id = queue[cursor];
    for (const next of downstream.get(id)!) {
      depth.set(next, Math.max(depth.get(next)!, depth.get(id)! + 1));
      indegree.set(next, indegree.get(next)! - 1);
      if (indegree.get(next) === 0) {
        queue.push(next);
        placed.add(next);
      }
    }
  }

  const stuck = ids.filter((id) => !placed.has(id));
  const rows = Math.max(...[...depth.values()], -1) + 1;
  const layers: string[][] = Array.from({ length: rows }, () => []);
  // Declaration order inside a row, so two runs of the same graph read the same.
  for (const id of ids) {
    if (placed.has(id)) layers[depth.get(id)!].push(id);
  }
  if (stuck.length > 0) layers.push(stuck);
  return layers.filter((layer) => layer.length > 0);
}

/** The nodes this one waits for, in declaration order. */
export function upstreamOf(graph: WorkflowGraph, nodeId: string): string[] {
  const seen = new Set<string>();
  for (const edge of graph.edges) {
    if (edge.to === nodeId && edge.from !== nodeId) seen.add(edge.from);
  }
  return graph.nodes.map((node) => node.id).filter((id) => seen.has(id));
}

/** Arguments as flat rows; objects and arrays are shown as compact JSON. */
export function argumentRows(args: Record<string, unknown>): [string, string][] {
  return Object.entries(args).map(([key, value]) => [
    key,
    typeof value === "string" ? value : JSON.stringify(value),
  ]);
}

/**
 * Node states in plain words. "Parked" rather than "waiting for approval"
 * because that is the word the run list, the badge and the ADR all use for a
 * node that stopped and is waiting for a person.
 */
export const NODE_STATUS_LABELS: Record<WorkflowNodeRunStatus, string> = {
  pending: "Not started",
  running: "Running",
  waiting_for_approval: "Parked for approval",
  succeeded: "Done",
  failed: "Failed",
  skipped: "Skipped",
};

export const RUN_STATUS_LABELS: Record<WorkflowRunStatus, string> = {
  queued: "Queued",
  running: "Running",
  waiting_for_approval: "Waiting for approval",
  succeeded: "Succeeded",
  failed: "Failed",
  cancelled: "Cancelled",
};

/** A run that is still going is a run worth re-fetching. */
export const RUN_TERMINAL: WorkflowRunStatus[] = ["succeeded", "failed", "cancelled"];

export function runIsSettled(status: string): boolean {
  return (RUN_TERMINAL as string[]).includes(status);
}

/**
 * A graph stopped on spend is not a graph waiting for a decision.
 *
 * ADR 0008 made a budget park reuse `waiting_for_approval` — six guards already
 * read that status as "stopped, waiting on a person, resumable" and are right
 * about a budget park too — so the status alone can no longer name the state.
 * `paused_reason` is what tells them apart, and getting it wrong here is not
 * cosmetic: "Waiting for approval" sends its owner hunting for a card that was
 * deliberately never written, which is exactly the undecidable-approval failure
 * this surface has already been through once.
 *
 * The word is *held* rather than *waiting*: nobody can decide this one, and
 * what unblocks it is a number, not a person's answer.
 */
export function isBudgetPark(status: string, pausedReason = ""): boolean {
  return status === "waiting_for_approval" && pausedReason === PAUSED_FOR_BUDGET;
}

/**
 * Three things can say why a run stopped, and this is the order they are
 * believed in. The order is the whole content of this function, so it lives
 * here where a test can pin it rather than inline in the view.
 *
 * 1. **A proposed `AgentToolCall` on the run.** Structural and decisive:
 *    `_park_for_budget` writes none, so a run that has one is parked on a
 *    person. It outranks the field because the field is a *mirror* — the API
 *    copies the backing run's reason onto `WorkflowRun` — and a mirror can lag.
 *    A graph released from the ceiling that walks on and parks on a write is
 *    exactly that case, and believing the stale "budget" renders the spend
 *    panel *instead of* the approval card: the proposal is on screen with
 *    nothing to decide it with.
 * 2. **`paused_reason` itself**, when the run carries one.
 * 3. **The ceiling's own list of held runs**, for a run that reported no reason
 *    at all — an older API, or a row written before the column existed.
 */
export function resolvePausedReason(
  run: { status: string; paused_reason: string; run_id: string | null },
  signals: { proposed: boolean; heldByBudget: boolean },
): string {
  if (signals.proposed) return PAUSED_FOR_APPROVAL;
  if (run.paused_reason) return run.paused_reason;
  if (run.status !== "waiting_for_approval" || !run.run_id) return "";
  return signals.heldByBudget ? PAUSED_FOR_BUDGET : "";
}

const BUDGET_RUN_LABEL = "Held by the spend limit";
const BUDGET_NODE_LABEL = "Held by the spend limit";

export function nodeStatusLabel(status: string, pausedReason = ""): string {
  if (isBudgetPark(status, pausedReason)) return BUDGET_NODE_LABEL;
  return NODE_STATUS_LABELS[status as WorkflowNodeRunStatus] ?? status;
}

export function runStatusLabel(status: string, pausedReason = ""): string {
  if (isBudgetPark(status, pausedReason)) return BUDGET_RUN_LABEL;
  return RUN_STATUS_LABELS[status as WorkflowRunStatus] ?? status;
}

/**
 * What a compile error means, in a sentence a person can act on.
 *
 * The API already sends a message written for a human; this is the headline
 * above it, because a bare `tool_unknown` is a stack trace with better spelling
 * and a failed compile is the *common* case here, not the exceptional one.
 */
const COMPILE_ERROR_TITLES: Record<string, string> = {
  schema_invalid: "The plan came back in the wrong shape",
  graph_empty: "The plan has no steps",
  graph_too_large: "The plan has too many steps",
  graph_cycle: "The steps depend on each other in a loop",
  node_id_invalid: "A step has an unusable name",
  node_id_reserved: "A step used a reserved name",
  node_id_duplicate: "Two steps share a name",
  edge_self_loop: "A step depends on itself",
  edge_unknown_node: "A dependency names a step that does not exist",
  edge_duplicate: "The same dependency is listed twice",
  trigger_cron_invalid: "The schedule is not a valid cron expression",
  trigger_cron_unexpected: "A manual workflow cannot carry a schedule",
  agent_prompt_missing: "An assistant step has nothing to do",
  agent_node_names_tool: "An assistant step also named a tool",
  agent_node_has_arguments: "An assistant step carries tool arguments",
  tool_node_has_prompt: "A tool step also carries a prompt",
  tool_missing: "A step does not say which tool to use",
  tool_unknown: "That tool does not exist in this workspace",
  arguments_invalid: "The arguments do not match what the tool accepts",
  reference_unknown_node: "A step reads from a step that does not exist",
  reference_not_upstream: "A step reads from a step that runs later",
};

const COMPILE_WARNING_TITLES: Record<string, string> = {
  tool_write_capable: "This step writes, so it will park for approval",
  tool_schema_invalid: "This tool's own schema is unreadable, so its arguments went unchecked",
};

export function compileErrorTitle(code: string): string {
  return COMPILE_ERROR_TITLES[code] ?? humanizeCode(code);
}

export function compileWarningTitle(code: string): string {
  return COMPILE_WARNING_TITLES[code] ?? humanizeCode(code);
}

function humanizeCode(code: string): string {
  const words = code.replace(/_/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : "Compilation failed";
}

/** Group findings by the step they blame, graph-level ones first. */
export function groupFindings(
  findings: WorkflowCompileFinding[],
): { node: string; items: WorkflowCompileFinding[] }[] {
  const order: string[] = [];
  const buckets = new Map<string, WorkflowCompileFinding[]>();
  for (const finding of findings) {
    if (!buckets.has(finding.node)) {
      buckets.set(finding.node, []);
      order.push(finding.node);
    }
    buckets.get(finding.node)!.push(finding);
  }
  // "" is the whole graph rather than a step, and reads first.
  order.sort((a, b) => (a === "" ? -1 : b === "" ? 1 : 0));
  return order.map((node) => ({ node, items: buckets.get(node)! }));
}

export type ReviewabilityNote = {
  /** parks: a write is visible. opaque: unknowable. unattended: read-only. */
  tone: "parks" | "opaque" | "unattended";
  headline: string;
  text: string;
};

/**
 * "What will this do?" — answered as far as it can honestly be answered.
 *
 * `requires_approval` is computed from the *tool* nodes, because those are the
 * only ones whose calls are known before the run. An agent node picks its tools
 * at run time, so a graph containing one has no static answer at all, and
 * saying "read-only, runs unattended" about it is a lie the graph cannot back
 * up. ADR 0007: "A UI that renders both node kinds the same way is lying about
 * that." Reporting a third state is what stops it.
 */
export function describeReviewability(
  graph: Pick<WorkflowGraph, "nodes">,
  requiresApproval: boolean,
): ReviewabilityNote {
  if (requiresApproval) {
    return {
      tone: "parks",
      headline: "Stops for a person before it writes",
      text: "A step here names a write-capable tool, so the run parks rather than doing it.",
    };
  }
  if (graph.nodes.some((node) => node.kind === "agent")) {
    return {
      tone: "opaque",
      headline: "What this does cannot be read off the graph",
      text:
        "No named step writes, but an assistant step chooses its own tools when " +
        "it runs. It still stops for a person if it reaches a write.",
    };
  }
  return {
    tone: "unattended",
    headline: "Read-only — runs unattended",
    text: "Every step names a tool that only reads, so nothing here needs a decision.",
  };
}

export type ScheduleNote = {
  /** Drives the tone of the panel: mint when it fires, clay when it does not. */
  tone: "quiet" | "warn" | "live";
  headline: string;
  detail: string;
};

/**
 * The truth about when this runs.
 *
 * `schedulingEnabled` is what the ticker itself said: `null` when we could not
 * ask. Every branch that cannot promise a dispatch says so in the headline, not
 * in small print, because ADR 0007's whole objection is that "it will run every
 * Monday" is a promise and a promise the system does not keep is worse than a
 * missing feature.
 */
export function describeSchedule(
  workflow: Pick<
    Workflow,
    "trigger_kind" | "schedule_cron" | "schedule_timezone" | "status" | "last_dispatched_at"
  >,
  schedulingEnabled: boolean | null,
): ScheduleNote {
  if (workflow.trigger_kind !== "schedule" || !workflow.schedule_cron) {
    return {
      tone: "quiet",
      headline: "Runs when you start it",
      detail: "No schedule. Press Run to execute the graph now.",
    };
  }
  // Shown verbatim. Rewriting "0 9 * * 1" as "every Monday at 09:00" is one
  // more place to be wrong about the thing this note exists to be right about.
  const when = `${workflow.schedule_cron} · ${workflow.schedule_timezone}`;
  if (schedulingEnabled === false) {
    return {
      tone: "warn",
      headline: `Schedule stored, but nothing will fire it`,
      detail:
        `This deployment has no workflow cron configured, so ${when} is a ` +
        "recorded intention and not a promise. Until an operator sets " +
        "WORKFLOW_CRON_SECRET and points a cron at the tick endpoint, this " +
        "workflow only runs when someone presses Run.",
    };
  }
  if (schedulingEnabled === null) {
    return {
      tone: "warn",
      headline: "Schedule stored; the scheduler did not answer",
      detail:
        `${when}. We could not reach the tick endpoint to find out whether ` +
        "anything dispatches it, so treat this as unscheduled until it answers.",
    };
  }
  if (workflow.status !== "active") {
    return {
      tone: "warn",
      headline: `Schedule stored, but this workflow is ${workflow.status}`,
      detail: `Only an active workflow is dispatched. Activate it to run on ${when}.`,
    };
  }
  return {
    tone: "live",
    headline: `Runs on ${when}`,
    detail: workflow.last_dispatched_at
      ? `Scheduling is configured. Last dispatched ${new Date(
          workflow.last_dispatched_at,
        ).toLocaleString()}.`
      : "Scheduling is configured. It has not fired yet.",
  };
}
