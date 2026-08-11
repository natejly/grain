"use client";

import type {
  WorkflowCompileFinding,
  WorkflowGraph,
  WorkflowNodeRun,
} from "@workspace/api-client";
import {
  ArrowDown,
  Ban,
  Bot,
  CircleCheck,
  CircleDashed,
  CirclePause,
  LoaderCircle,
  ShieldQuestion,
  SkipForward,
  TriangleAlert,
  Wrench,
} from "lucide-react";
import { Fragment, type ReactNode } from "react";
import {
  argumentRows,
  compileWarningTitle,
  isBudgetPark,
  layerGraph,
  nodeStatusLabel,
  upstreamOf,
} from "./workflow-format";

/**
 * The graph, drawn as rows rather than as a canvas.
 *
 * A workflow here is a dozen nodes executed once in topological order, and the
 * question a reader actually has is "what will this call, with what, and in
 * which order" — which a top-to-bottom list answers better than a diagram, and
 * without a layout engine, a viewport, or a dependency. Nodes that can run at
 * the same time share a row; an arrow separates rows.
 *
 * Two things a picture alone would get wrong, so both are written out:
 *
 * **Skip edges.** A node three rows down may depend on the first one. The rows
 * cannot show that, so every node names its dependencies in words.
 *
 * **The two node kinds.** ADR 0007: "A UI that renders both node kinds the same
 * way is lying about that." A tool node lists every call it will make. An agent
 * node cannot — it picks its tools at run time — so it says so, in the card,
 * every time.
 *
 * The same component draws a compile preview and a live run: pass `nodeRuns`
 * and each card gains its state, its output and, if it parked, the decision.
 * One picture for "what this does" and "what it did" is the point.
 */
export type WorkflowGraphViewProps = {
  graph: WorkflowGraph;
  /** Per-node run state keyed by node id, when a run is being watched. */
  nodeRuns?: Record<string, WorkflowNodeRun>;
  /** Compile warnings keyed by node id — which steps write, and so will park. */
  warnings?: Record<string, WorkflowCompileFinding[]>;
  /** Rendered inside a parked node, so the decision sits on the step itself. */
  renderApproval?: (node: WorkflowNodeRun) => ReactNode;
  /**
   * The watched run's `paused_reason`, when one is being watched.
   *
   * It belongs to the run rather than the node — the executor mirrors the park
   * onto the workflow run and leaves the node row saying only
   * `waiting_for_approval` — so a node cannot tell on its own whether it is
   * waiting for a decision or held by the ceiling, and is told from here.
   */
  pausedReason?: string;
};

function StatusPill({ status, pausedReason }: { status: string; pausedReason: string }) {
  const held = isBudgetPark(status, pausedReason);
  const Icon =
    status === "succeeded"
      ? CircleCheck
      : status === "failed"
        ? Ban
        : status === "running"
          ? LoaderCircle
          : status === "waiting_for_approval"
            ? held
              ? CirclePause
              : ShieldQuestion
            : status === "skipped"
              ? SkipForward
              : CircleDashed;
  return (
    <span className={`workflow-node-status ${held ? "budget" : status}`}>
      <Icon size={13} className={status === "running" ? "spin" : undefined} />
      {nodeStatusLabel(status, pausedReason)}
    </span>
  );
}

export function WorkflowGraphView({
  graph,
  nodeRuns,
  warnings,
  renderApproval,
  pausedReason = "",
}: WorkflowGraphViewProps) {
  const layers = layerGraph(graph);
  const byId = new Map(graph.nodes.map((node) => [node.id, node]));

  return (
    <ol className="workflow-graph">
      {layers.map((layer, index) => (
        <Fragment key={`layer-${index}`}>
          {index > 0 && (
            <li className="workflow-flow" aria-hidden="true">
              <ArrowDown size={15} />
            </li>
          )}
          <li className="workflow-layer">
            <ol className="workflow-layer-nodes">
              {layer.map((id) => {
                const node = byId.get(id);
                if (!node) return null;
                const run = nodeRuns?.[id];
                const after = upstreamOf(graph, id);
                const notes = warnings?.[id] ?? [];
                // An agent node's only "argument" is its prompt, which the card
                // already shows — resolved once the run has substituted its
                // references, and as the template before that. Listing it twice
                // was just the same sentence in two type sizes.
                const resolved = run?.arguments.prompt;
                const prompt =
                  node.kind === "agent" && typeof resolved === "string"
                    ? resolved
                    : node.prompt;
                const args =
                  node.kind === "agent"
                    ? []
                    : argumentRows(
                        run && Object.keys(run.arguments).length > 0
                          ? run.arguments
                          : node.arguments,
                      );
                return (
                  <li key={id}>
                    <article
                      className={`workflow-node ${node.kind}${
                        run ? ` ran ${run.status}` : ""
                      }`}
                    >
                      <header className="workflow-node-head">
                        <span className="workflow-node-kind">
                          {node.kind === "agent" ? <Bot size={13} /> : <Wrench size={13} />}
                          {node.kind === "agent" ? "Assistant" : "Tool"}
                        </span>
                        <strong>{node.id}</strong>
                        {run && (
                          <StatusPill status={run.status} pausedReason={pausedReason} />
                        )}
                      </header>

                      {node.kind === "tool" ? (
                        <code className="workflow-node-tool">{node.tool}</code>
                      ) : (
                        <p className="workflow-node-prompt">{prompt}</p>
                      )}

                      {node.description && (
                        <p className="workflow-node-why">{node.description}</p>
                      )}

                      {after.length > 0 && (
                        <p className="workflow-node-after">
                          After {after.join(", ")}
                        </p>
                      )}

                      {node.kind === "agent" && (
                        <p className="workflow-node-caveat">
                          Chooses its own tools when it runs, so this card cannot list
                          them. Every call it makes is still checked against this
                          workspace&rsquo;s policy and parks for approval if it writes.
                        </p>
                      )}

                      {args.length > 0 && (
                        <dl className="workflow-node-args">
                          {args.map(([key, value]) => (
                            <div key={key}>
                              <dt>{key}</dt>
                              <dd>{value}</dd>
                            </div>
                          ))}
                        </dl>
                      )}

                      {notes.map((note, position) => (
                        <p className="workflow-node-warning" key={`${note.code}-${position}`}>
                          <TriangleAlert size={13} />
                          <span>{compileWarningTitle(note.code)}</span>
                        </p>
                      ))}

                      {run && run.output && (
                        <div className="workflow-node-output">
                          <span className="workflow-node-label">Output</span>
                          <pre>{run.output}</pre>
                        </div>
                      )}

                      {run && run.error && (
                        <p className="workflow-node-error">{run.error}</p>
                      )}

                      {run && (run.latency_ms > 0 || run.policy) && (
                        <p className="workflow-node-meta">
                          {run.latency_ms > 0 ? `${run.latency_ms}ms` : ""}
                          {run.latency_ms > 0 && run.policy ? " · " : ""}
                          {run.policy === "allow"
                            ? "ran without asking"
                            : run.policy === "ask"
                              ? "a person authorised this call"
                              : run.policy === "deny"
                                ? "refused by policy"
                                : run.policy === "agent"
                                  ? "assistant turn"
                                  : ""}
                        </p>
                      )}

                      {run && renderApproval?.(run)}
                    </article>
                  </li>
                );
              })}
            </ol>
          </li>
        </Fragment>
      ))}
    </ol>
  );
}
