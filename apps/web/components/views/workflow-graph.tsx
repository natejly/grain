"use client";

import type {
  WorkflowCompileFinding,
  WorkflowGraph,
  WorkflowGraphNode,
  WorkflowNodeRun,
} from "@workspace/api-client";
import {
  Background,
  BackgroundVariant,
  Handle,
  MarkerType,
  Panel,
  Position,
  ReactFlow,
  useReactFlow,
  type Edge,
  type Node,
  type NodeChange,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/base.css";
import {
  Ban,
  Bot,
  ChevronDown,
  CircleCheck,
  CircleDashed,
  CirclePause,
  LoaderCircle,
  Maximize2,
  ShieldQuestion,
  SkipForward,
  TriangleAlert,
  UserRound,
  Wrench,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  argumentRows,
  compileWarningTitle,
  inputLabel,
  isBudgetPark,
  layerGraph,
  nodeStatusLabel,
  upstreamOf,
} from "./workflow-format";

/**
 * The graph, drawn on a dot-grid canvas.
 *
 * Layout is not the library's: `layerGraph` is the same Kahn's-algorithm walk
 * the executor's order comes from, so a node sits one row below its deepest
 * dependency and two steps that can run at once sit side by side. React Flow is
 * here for the viewport — edge routing, pan, zoom, and the pointer and keyboard
 * handling that goes with them — and not to decide what goes where.
 *
 * **A node is a chip until you look at it.** The canvas answers "what are the
 * steps and in what order" at a glance, and everything else — the prompt, the
 * arguments, the dependencies, the output, the decision — is one hover or one
 * Tab away, on the node itself. Nothing about a step requires leaving the
 * canvas to read, which is what the layered rows bought by being enormous.
 *
 * Three things a picture alone would get wrong, so all three are written out:
 *
 * **Skip edges.** An edge is drawn, but a node three rows down that depends on
 * the first one is easy to misread, so every node also names its dependencies.
 *
 * **The two node kinds.** ADR 0007: "A UI that renders both node kinds the same
 * way is lying about that." A tool node lists every call it will make. An agent
 * node cannot — it picks its tools at run time — so it says so, on the chip and
 * again in full when opened.
 *
 * **A node that stopped the run is pinned open.** A parked step holds the
 * decision and a failed one holds the reason, and neither may be something a
 * person has to go looking for — or worse, something that disappears when the
 * pointer moves. Hover reveals; stopping pins.
 *
 * The same component draws a compile preview and a live run: pass `nodeRuns`
 * and each chip gains its state, its output and, if it parked, the decision.
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

/** Chip width and row pitch, in flow units. The dot grid is 20, so both align. */
const NODE_WIDTH = 240;
const COLUMN_PITCH = 280;
const ROW_PITCH = 180;

/**
 * Roughly what an opened chip occupies. Only ever a fallback: framing measures
 * the real thing, and reaches for these two when there is nothing to measure.
 */
const CHIP_HEIGHT = 104;
const DETAIL_HEIGHT = 260;

/**
 * The resting view of the whole graph, and the button that returns to it.
 *
 * Lopsided on purpose: a chip opens *downward*, so the bottom row needs room
 * below it or its detail opens straight into the canvas edge. Hover, unlike a
 * click, deliberately does not move the camera to rescue it — this is what
 * makes that restraint affordable.
 */
const FIT_VIEW = {
  padding: { top: "12%", right: "12%", bottom: "34%", left: "12%" },
  maxZoom: 1,
} as const;

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

type StepData = {
  node: WorkflowGraphNode;
  run: WorkflowNodeRun | null;
  after: string[];
  warnings: WorkflowCompileFinding[];
  pausedReason: string;
  approval: ReactNode;
  /** A step that stopped the run: open, and not closeable by moving away. */
  pinned: boolean;
  [key: string]: unknown;
};

type StepNode = Node<StepData, "step">;

function StepChip({ data, positionAbsoluteX, positionAbsoluteY }: NodeProps<StepNode>) {
  const { node, run, after, warnings, pausedReason, approval, pinned } = data;
  // Sticky is the click: a person reading three steps in turn should not have
  // to keep the pointer inside a 240px box to keep the answer on screen.
  const [sticky, setSticky] = useState(false);
  const [pointer, setPointer] = useState(false);
  const [focus, setFocus] = useState(false);
  const open = pinned || sticky || pointer || focus;

  /**
   * Brings this chip, and all of what it opened, into the frame.
   *
   * Measured rather than estimated. A parked step's card carries a preview of
   * the write it wants to make, so its height is the length of somebody else's
   * document — guess it low and the Approve button is painted outside a canvas
   * that clips its overflow, which is an approval nobody can give and, worse,
   * one that looks like a run that simply stopped. `offsetHeight` is layout
   * pixels and ignores the viewport's scale, which is exactly what `fitBounds`
   * wants: it works out the zoom, so a card too tall for the canvas is shown
   * smaller rather than shown in part.
   */
  const flow = useReactFlow();
  const chipRef = useRef<HTMLElement | null>(null);
  const detailRef = useRef<HTMLDivElement | null>(null);
  const frame = useCallback(() => {
    void flow.fitBounds(
      {
        x: positionAbsoluteX,
        y: positionAbsoluteY,
        // Wider than the chip for a step that stopped the run, so the diff it
        // is asking about is readable — measured for the same reason as the
        // height, since a guess is what puts half of it outside the frame.
        width: Math.max(NODE_WIDTH, detailRef.current?.offsetWidth ?? 0),
        height:
          (chipRef.current?.offsetHeight ?? CHIP_HEIGHT) +
          (detailRef.current?.offsetHeight ?? DETAIL_HEIGHT),
      },
      { padding: 0.08, duration: 220 },
    );
  }, [flow, positionAbsoluteX, positionAbsoluteY]);

  // Two deliberate moments, and only those two. A step that stopped the run is
  // framed because that is where the person now has to look, and a *clicked*
  // chip is framed because clicking is a request to read it. Hover is not on
  // the list: a camera that moved whenever a pointer crossed the canvas would
  // be unusable, and panning out from under a hovering pointer would collapse
  // the very chip the pan was for.
  useEffect(() => {
    const detail = detailRef.current;
    if (!pinned || !detail) return;
    frame();
    // Re-framed when the card *changes size*, not on every render: the parent
    // re-renders every 1.2 seconds while a run is open, and a viewport that
    // re-centred on each poll would fight anyone trying to look elsewhere. The
    // size does change once and late — a park renders before the approval it is
    // waiting on has been fetched — and that is the moment worth reacting to.
    if (typeof ResizeObserver === "undefined") return;
    const watch = new ResizeObserver(() => frame());
    watch.observe(detail);
    return () => watch.disconnect();
  }, [pinned, frame]);
  useEffect(() => {
    if (sticky) frame();
  }, [sticky, frame]);

  // An agent node's only "argument" is its prompt, which the card already shows
  // — resolved once the run has substituted its references, and as the template
  // before that. Listing it twice was the same sentence in two type sizes.
  const resolved = run?.arguments.prompt;
  const prompt =
    node.kind === "agent" && typeof resolved === "string" ? resolved : node.prompt;
  // Only a tool node has arguments to list. An agent node's are its prompt,
  // shown above; a manual node's "inputs" are the fields it asks a person for,
  // which the detail panel lists on their own.
  const args =
    node.kind === "tool"
      ? argumentRows(
          run && Object.keys(run.arguments).length > 0 ? run.arguments : node.arguments,
        )
      : [];

  return (
    <article
      className={`workflow-node ${node.kind}${run ? ` ran ${run.status}` : ""}${
        open ? " open" : ""
      }`}
      ref={chipRef}
      style={{ width: NODE_WIDTH }}
      onPointerEnter={() => setPointer(true)}
      onPointerLeave={() => setPointer(false)}
    >
      <Handle type="target" position={Position.Top} isConnectable={false} />

      <button
        type="button"
        className="workflow-node-summary"
        aria-expanded={open}
        onClick={() => setSticky((current) => !current)}
        onFocus={() => setFocus(true)}
        onBlur={() => setFocus(false)}
      >
        <span className="workflow-node-head">
          <span className="workflow-node-kind">
            {node.kind === "agent" ? (
              <Bot size={13} />
            ) : node.kind === "manual" ? (
              <UserRound size={13} />
            ) : (
              <Wrench size={13} />
            )}
            {node.kind === "agent" ? "Assistant" : node.kind === "manual" ? "Manual" : "Tool"}
          </span>
          <strong>{node.id}</strong>
          <ChevronDown size={13} className="workflow-node-caret" aria-hidden="true" />
        </span>
        {node.kind === "tool" ? (
          <code className="workflow-node-tool">{node.tool}</code>
        ) : (
          <span className="workflow-node-gist">{prompt}</span>
        )}
        {run && <StatusPill status={run.status} pausedReason={pausedReason} />}
        {warnings.length > 0 && (
          <span className="workflow-node-warning">
            <TriangleAlert size={13} />
            <span>{compileWarningTitle(warnings[0].code)}</span>
          </span>
        )}
      </button>

      {open && (
        <div className="workflow-node-detail nowheel" ref={detailRef}>
          {/* Why a step stopped the run, and what to do about it, lead the
              panel. The panel scrolls — it has to, an output can be any length
              — and a decision below its fold is a decision nobody knows they
              can make: the run just sits there looking stuck. Reading order is
              the only priority signal a scrolled box has, so the two things
              that stopped the run get the top of it and the description of a
              step that has already run gets the bottom. */}
          {run && run.error && <p className="workflow-node-error">{run.error}</p>}
          {approval}

          {node.description && <p className="workflow-node-why">{node.description}</p>}

          {node.kind === "agent" && (
            <>
              <p className="workflow-node-label">Prompt</p>
              <p className="workflow-node-prompt">{prompt}</p>
              <p className="workflow-node-caveat">
                Chooses its own tools when it runs, so this card cannot list them.
                Every call it makes is still checked against this workspace&rsquo;s
                policy and parks for approval if it writes.
              </p>
            </>
          )}

          {node.kind === "manual" && (
            <>
              <p className="workflow-node-label">Asks a person</p>
              <p className="workflow-node-prompt">{node.prompt}</p>
              {node.fields && node.fields.length > 0 ? (
                <>
                  <p className="workflow-node-label">Collects</p>
                  <dl className="workflow-node-args">
                    {node.fields.map((field) => (
                      <div key={field.name}>
                        <dt>{inputLabel(field)}</dt>
                        <dd>
                          {field.type}
                          {field.required ? "" : " · optional"}
                        </dd>
                      </div>
                    ))}
                  </dl>
                </>
              ) : (
                <p className="workflow-node-caveat">
                  Collects nothing — its output is an empty object. The run pauses
                  here until a person proceeds or rejects.
                </p>
              )}
            </>
          )}

          {args.length > 0 && (
            <>
              <p className="workflow-node-label">Inputs</p>
              <dl className="workflow-node-args">
                {args.map(([key, value]) => (
                  <div key={key}>
                    <dt>{key}</dt>
                    <dd>{value}</dd>
                  </div>
                ))}
              </dl>
            </>
          )}

          <p className="workflow-node-after">
            {after.length > 0 ? `After ${after.join(", ")}` : "Starts the workflow"}
          </p>

          {warnings.slice(1).map((note, position) => (
            <p className="workflow-node-warning" key={`${note.code}-${position}`}>
              <TriangleAlert size={13} />
              <span>{compileWarningTitle(note.code)}</span>
            </p>
          ))}

          {run && run.output != null && run.output !== "" && (
            <>
              <p className="workflow-node-label">Output</p>
              <pre className="workflow-node-output">
                {typeof run.output === "string"
                  ? run.output
                  : JSON.stringify(run.output, null, 2)}
              </pre>
            </>
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
        </div>
      )}

      <Handle type="source" position={Position.Bottom} isConnectable={false} />
    </article>
  );
}

/** Defined once: React Flow remounts every node when this object changes. */
const NODE_TYPES = { step: StepChip };

/**
 * Zoom, and a way back to the whole graph.
 *
 * Hand-built rather than React Flow's `<Controls>` because these need
 * accessible names and this workspace's button styles, and the vendor's carry
 * neither — the stylesheet that would give them a look is also the one that
 * hardcodes a palette.
 */
function CanvasControls() {
  const flow = useReactFlow();
  return (
    <Panel position="top-right" className="workflow-canvas-controls">
      <button
        type="button"
        className="icon-button"
        aria-label="Zoom in"
        onClick={() => void flow.zoomIn()}
      >
        <ZoomIn size={15} />
      </button>
      <button
        type="button"
        className="icon-button"
        aria-label="Zoom out"
        onClick={() => void flow.zoomOut()}
      >
        <ZoomOut size={15} />
      </button>
      <button
        type="button"
        className="icon-button"
        aria-label="Fit the whole graph"
        onClick={() => void flow.fitView(FIT_VIEW)}
      >
        <Maximize2 size={15} />
      </button>
    </Panel>
  );
}

export function WorkflowGraphView({
  graph,
  nodeRuns,
  warnings,
  renderApproval,
  pausedReason = "",
}: WorkflowGraphViewProps) {
  const approvalOf = useCallback(
    (run: WorkflowNodeRun | null): ReactNode =>
      run ? (renderApproval?.(run) ?? null) : null,
    [renderApproval],
  );

  /**
   * The sizes React Flow measured, carried across rebuilds of the node array.
   *
   * `adoptUserNodes` reads `measured` off the object it is handed, and a node
   * arriving without it counts as unmeasured — rendered `visibility: hidden`
   * until something re-measures it. Nothing does: the element's size never
   * changed, so the ResizeObserver stays quiet. Meanwhile the array is rebuilt
   * on every parent render, because a watched run hands down a fresh
   * `nodeRuns` object each poll. Without this, a step that was on screen
   * disappears the moment it finishes — the canvas keeps its dot grid and its
   * zoom controls and draws nothing at all.
   */
  const measured = useRef(new Map<string, { width: number; height: number }>());
  const trackDimensions = useCallback((changes: NodeChange<StepNode>[]) => {
    for (const change of changes) {
      if (change.type === "dimensions" && change.dimensions) {
        measured.current.set(change.id, change.dimensions);
      }
    }
  }, []);

  const nodes = useMemo<StepNode[]>(() => {
    const byId = new Map(graph.nodes.map((node) => [node.id, node]));
    const layers = layerGraph(graph);
    return layers.flatMap((layer, row) =>
      layer.flatMap((id, column) => {
        const node = byId.get(id);
        if (!node) return [];
        const run = nodeRuns?.[id] ?? null;
        const pinned =
          run?.status === "waiting_for_approval" || run?.status === "failed";
        return [
          {
            id,
            type: "step" as const,
            position: { x: column * COLUMN_PITCH, y: row * ROW_PITCH },
            measured: measured.current.get(id),
            // A chip's detail hangs *downward*, over the row beneath it, so
            // earlier rows have to paint over later ones — otherwise opening
            // the first step puts half its answer behind the second. Ordering
            // by row does that without the parent having to know which chip is
            // open, which it cannot: hovering is the chip's own business.
            // A stopped step outranks all of them; it is the one being read.
            zIndex: pinned ? layers.length + 10 : layers.length - row,
            // React Flow decides a node is inert — `pointer-events: none`, set
            // inline — unless it is draggable, selectable, or carries one of
            // *its* mouse handlers. This canvas is none of those: a chip is
            // opened by hovering the chip itself, not by a graph-level
            // callback, so without this the pane swallows every pointer and
            // nothing ever expands. `node.style` is spread after that default.
            style: { pointerEvents: "all" },
            data: {
              node,
              run,
              after: upstreamOf(graph, id),
              warnings: warnings?.[id] ?? [],
              pausedReason,
              approval: approvalOf(run),
              pinned,
            },
          },
        ];
      }),
    );
  }, [graph, nodeRuns, warnings, pausedReason, approvalOf]);

  const edges = useMemo<Edge[]>(() => {
    const known = new Set(graph.nodes.map((node) => node.id));
    return graph.edges
      .filter((edge) => known.has(edge.from) && known.has(edge.to) && edge.from !== edge.to)
      .map((edge) => ({
        id: `${edge.from}-${edge.to}`,
        source: edge.from,
        target: edge.to,
        type: "smoothstep",
        markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14 },
      }));
  }, [graph]);

  // A run that stopped puts a decision on the canvas, and a parked step's card
  // carries a preview of the write it wants to make — several times a chip's
  // height. The canvas takes the room for it, rather than making a person find
  // a scrollbar inside a 240px box to reach an Approve button.
  const stopped = nodes.some((node) => node.data.pinned);

  if (graph.nodes.length === 0) {
    return <p className="workflow-empty">This graph has no steps.</p>;
  }

  return (
    // The name goes on a role, not on React Flow's root: that root is a plain
    // div, and an aria-label on a generic element names nothing.
    <div
      className={stopped ? "workflow-canvas stopped" : "workflow-canvas"}
      role="group"
      aria-label={`${graph.name || "Workflow"} steps`}
    >
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={trackDimensions}
        nodeTypes={NODE_TYPES}
        // The graph is compiled, not drawn: moving a node would say a position
        // means something, and re-running would put it back.
        nodesDraggable={false}
        nodesConnectable={false}
        nodesFocusable={false}
        edgesFocusable={false}
        elementsSelectable={false}
        panOnScroll
        zoomOnDoubleClick={false}
        minZoom={0.3}
        maxZoom={1.6}
        fitView
        fitViewOptions={FIT_VIEW}
        proOptions={{ hideAttribution: false }}
      >
        <Background variant={BackgroundVariant.Dots} gap={20} size={1.4} />
        <CanvasControls />
      </ReactFlow>
    </div>
  );
}
