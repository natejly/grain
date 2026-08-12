"use client";

import {
  WorkflowCompileError,
  WorkflowInputError,
  type AgentInfo,
  type AgentToolCall,
  type Workflow,
  type WorkflowCompileFinding,
  type WorkflowCompileResult,
  type WorkflowGraphNode,
  type WorkflowInputSpec,
  type WorkflowNodeRun,
  type WorkflowRun,
  type WorkflowRunDetail,
  type WorkflowStatus,
} from "@workspace/api-client";
import {
  Check,
  CircleSlash,
  LoaderCircle,
  Play,
  Plus,
  ShieldQuestion,
  Sparkles,
  Trash2,
  TriangleAlert,
  UserRound,
  Workflow as WorkflowIcon,
  X,
} from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { api } from "../api";
import { BudgetHold } from "./budget";
import { ProposalDiff } from "./document-pending";
import { describeError, formatRelative } from "./shared";
import {
  attributeProblems,
  buildTimeline,
  compileErrorTitle,
  describeReviewability,
  describeSchedule,
  formatDuration,
  groupFindings,
  inputLabel,
  isBudgetPark,
  nodeStatusLabel,
  resolvePausedReason,
  runIsSettled,
  runStatusLabel,
  type InputProblem,
} from "./workflow-format";
import { WorkflowGraphView } from "./workflow-graph";
import { WorkflowInputForm } from "./workflow-inputs";

/**
 * Workflow automations, end to end: describe one in English, read the graph the
 * compiler produced, save it, run it, and answer what it parked on.
 *
 * The panel owns its own data rather than joining the workspace hook, for the
 * same reason the sandbox does: nobody who is chatting needs a workflow's run
 * history fetched on every page load.
 *
 * Two things here are deliberate and easy to undo by accident.
 *
 * **Saving stores the graph you read, not the sentence again.** `create` is
 * given the compiled document, so the automation that lands in the database is
 * the one that was on screen. Passing the prompt back instead would run the
 * model a second time and store whatever it said that time, which makes the
 * review step decorative.
 *
 * **A failed compile is the common case.** The compiler checks every tool
 * against this workspace's registry, and a model that invents one is refused —
 * that is the feature working, so the refusal gets a panel with every finding
 * in it, not a toast with the first one.
 */
export type WorkflowsViewProps = {
  setError: (message: string) => void;
  /**
   * Set when the user picked "Workflow" from the Create menu. Consumed on the
   * first render that sees it, like the sandbox's start request, so returning
   * to this view later does not reopen a composer they closed.
   */
  composeRequested?: boolean;
  onComposeHandled?: () => void;
  /**
   * Called when this panel has changed something the rest of the shell holds a
   * list of. A workflow run approves a write, creates a document and resolves a
   * pending edit without any of the shell's own refresh paths hearing about it.
   */
  onWorkspaceChanged?: () => void;
};

/**
 * How many workflows are scanned for runs waiting on a human when the view
 * opens. A bound rather than a page: the answer to "is anything waiting for me"
 * has to arrive with the list, and a workspace with hundreds of automations
 * should not open with hundreds of requests.
 */
const INBOX_WORKFLOWS = 25;
const RUNS_PER_WORKFLOW = 20;

/**
 * Ties the header's "Run now" to the input form lower down the page.
 *
 * One button, in one place, whether or not the graph takes parameters — the
 * alternative was a Run button that moved when a workflow happened to declare
 * an input, which is a worse answer to "how do I start this".
 */
const RUN_FORM_ID = "workflow-run-form";

type RunMap = Record<string, WorkflowRun[]>;

function StatusChip({ status }: { status: WorkflowStatus }) {
  return <span className={`workflow-chip ${status}`}>{status}</span>;
}

/** Every finding the validator collected, as something a person can act on. */
function CompileFailure({
  errors,
  onDismiss,
}: {
  errors: WorkflowCompileFinding[];
  onDismiss: () => void;
}) {
  return (
    <section className="workflow-failure" role="alert">
      <header>
        <TriangleAlert size={16} />
        <div>
          <strong>That automation could not be built</strong>
          <span>
            The compiler checked every step against the tools this workspace
            actually has and refused the plan. Nothing was saved. Rewording the
            request — or naming the tool you meant — usually fixes it.
          </span>
        </div>
        <button className="icon-button" onClick={onDismiss} aria-label="Dismiss compile errors">
          <X size={16} />
        </button>
      </header>
      {groupFindings(errors).map((group) => (
        <div className="workflow-finding-group" key={group.node || "graph"}>
          <span className="workflow-finding-where">
            {group.node ? `Step “${group.node}”` : "The plan as a whole"}
          </span>
          {group.items.map((item, index) => (
            <div className="workflow-finding" key={`${item.code}-${index}`}>
              <strong>{compileErrorTitle(item.code)}</strong>
              <p>{item.message}</p>
            </div>
          ))}
        </div>
      ))}
    </section>
  );
}

function AuthorPanel({
  setError,
  onSaved,
}: {
  setError: (message: string) => void;
  onSaved: (workflow: Workflow) => void;
}) {
  const [prompt, setPrompt] = useState("");
  const [preview, setPreview] = useState<WorkflowCompileResult | null>(null);
  const [errors, setErrors] = useState<WorkflowCompileFinding[]>([]);
  const [busy, setBusy] = useState(false);

  async function compile() {
    if (!prompt.trim() || busy) return;
    setBusy(true);
    setErrors([]);
    setPreview(null);
    try {
      setPreview(await api.compileWorkflow(prompt.trim()));
    } catch (caught) {
      if (caught instanceof WorkflowCompileError) setErrors(caught.errors);
      else setError(describeError(caught, "Could not compile that automation"));
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    if (!preview || busy) return;
    setBusy(true);
    try {
      // The reviewed document, not the sentence: a second compile could produce
      // a different graph, and then the review was of something else.
      onSaved(
        await api.createWorkflow({
          graph: preview.graph,
          source_prompt: prompt.trim(),
          status: "draft",
        }),
      );
    } catch (caught) {
      if (caught instanceof WorkflowCompileError) setErrors(caught.errors);
      else setError(describeError(caught, "Could not save that workflow"));
    } finally {
      setBusy(false);
    }
  }

  // Which steps write, keyed by node, so the graph can mark them where they are.
  const warnings: Record<string, WorkflowCompileFinding[]> = {};
  for (const warning of preview?.summary.warnings ?? []) {
    if (!warning.node) continue;
    (warnings[warning.node] ??= []).push(warning);
  }
  const reviewability = preview
    ? describeReviewability(preview.graph, preview.summary.requires_approval)
    : null;

  return (
    <section className="workflow-author">
      <div className="page-heading">
        <div>
          <h1>New workflow</h1>
        </div>
      </div>

      <div className="workflow-compose">
        <textarea
          className="workflow-prompt"
          aria-label="Workflow prompt"
          // An example, not a label: it is the only thing on the page that
          // shows what a workflow description is allowed to look like.
          placeholder="Every Monday, pull the open pull requests, summarise them, and post the summary to Slack."
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          onKeyDown={(event) => {
            if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
              event.preventDefault();
              void compile();
            }
          }}
        />
        <div className="workflow-compose-actions">
          <button
            className="primary-button"
            onClick={() => void compile()}
            disabled={busy || !prompt.trim()}
          >
            {busy ? <LoaderCircle size={14} className="spin" /> : <Sparkles size={14} />}
            {busy ? "Compiling…" : "Compile"}
          </button>
        </div>
      </div>

      {errors.length > 0 && (
        <CompileFailure errors={errors} onDismiss={() => setErrors([])} />
      )}

      {preview && (
        <section className="workflow-preview">
          <div className="workflow-preview-head">
            <div>
              <strong>{preview.summary.name}</strong>
              <span>{preview.summary.description}</span>
            </div>
            <div className="workflow-preview-facts">
              <span>
                {preview.summary.node_count} step
                {preview.summary.node_count === 1 ? "" : "s"}
              </span>
              <span>
                {preview.summary.edge_count} link
                {preview.summary.edge_count === 1 ? "" : "s"}
              </span>
              {preview.summary.attempts > 1 && (
                <span>repaired after {preview.summary.attempts - 1}</span>
              )}
            </div>
          </div>

          {reviewability && (
            <p className={`workflow-approval-note ${reviewability.tone}`}>
              <ShieldQuestion size={14} />
              {reviewability.text}
            </p>
          )}

          {/* Declared parameters are part of "what is this" — a plan that asks
              for a repository name is a different plan from one that hardcodes
              one, and that has to be readable before it is saved. */}
          <InputDeclaration specs={preview.summary.inputs ?? preview.graph.inputs ?? []} />

          <WorkflowGraphView graph={preview.graph} warnings={warnings} />

          <div className="workflow-preview-actions">
            <button className="ghost-button" onClick={() => setPreview(null)} disabled={busy}>
              <X size={14} /> Discard
            </button>
            <button className="primary-button" onClick={() => void save()} disabled={busy}>
              <Check size={14} /> {busy ? "Saving…" : "Save workflow"}
            </button>
          </div>
        </section>
      )}
    </section>
  );
}

/** The parameters a compiled graph will ask for, before it is saved. */
function InputDeclaration({ specs }: { specs: WorkflowInputSpec[] }) {
  if (specs.length === 0) return null;
  return (
    <dl className="workflow-input-declaration">
      {specs.map((spec) => (
        <div key={spec.name}>
          <dt>{inputLabel(spec)}</dt>
          <dd>
            <span className="workflow-input-type">{spec.type}</span>
            {spec.required ? "required" : "optional"}
            {spec.default !== null && spec.default !== undefined
              ? ` · defaults to ${JSON.stringify(spec.default)}`
              : ""}
            {spec.choices.length > 0
              ? ` · one of ${spec.choices.map((choice) => String(choice)).join(", ")}`
              : ""}
          </dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * The nodes of a run against the clock: what ran, in the order it ran, how long
 * it took, and — for a step that produced output or failed — the detail on tap.
 *
 * The graph above answers "what is the shape of this workflow"; this answers
 * "what did this run actually do, and where did the time go". The two are
 * deliberately not the same view: a topological graph cannot show that step
 * three sat parked for a person for two minutes while step four ran in 40ms.
 */
function WorkflowRunTimeline({
  run,
  pausedReason,
}: {
  run: WorkflowRunDetail;
  pausedReason: string;
}) {
  const [openKey, setOpenKey] = useState<string | null>(null);
  const { rows, totalMs } = buildTimeline(run, run.nodes);
  if (!rows.length) return null;
  return (
    <div className="workflow-timeline">
      <div className="panel-title">
        <div>
          <strong>Timeline</strong>
        </div>
        <span className="panel-count">{rows.length}</span>
      </div>
      <ol className="workflow-timeline-rows">
        {rows.map(({ node, offsetMs, durationMs }) => {
          const held = isBudgetPark(node.status, pausedReason);
          const statusClass = held ? "budget" : node.status;
          const left = offsetMs === null ? 0 : (offsetMs / totalMs) * 100;
          const width = durationMs ? Math.max((durationMs / totalMs) * 100, 1.5) : 0;
          const hasOutput =
            node.output !== null && node.output !== undefined && node.output !== "";
          const hasDetail = Boolean(node.error) || hasOutput;
          const open = openKey === node.node_key;
          return (
            <li key={node.node_key} className={`workflow-timeline-row ${statusClass}`}>
              <button
                type="button"
                className="workflow-timeline-head"
                disabled={!hasDetail}
                aria-expanded={hasDetail ? open : undefined}
                onClick={() => setOpenKey(open ? null : node.node_key)}
              >
                <span className={`workflow-node-status ${statusClass}`}>
                  {nodeStatusLabel(node.status, pausedReason)}
                </span>
                <span className="workflow-timeline-name">
                  <code>{node.node_key}</code>
                  {node.attempt > 1 && <em> · attempt {node.attempt}</em>}
                </span>
                <span className="workflow-timeline-track" aria-hidden>
                  {offsetMs !== null && (
                    <span
                      className="workflow-timeline-bar"
                      style={{
                        insetInlineStart: `${left}%`,
                        width: width ? `${width}%` : undefined,
                      }}
                    />
                  )}
                </span>
                <span className="workflow-timeline-time">
                  {offsetMs === null ? "—" : `+${formatDuration(offsetMs)}`}
                  {durationMs ? ` · ${formatDuration(durationMs)}` : ""}
                </span>
              </button>
              {open && hasDetail && (
                <div className="workflow-timeline-detail">
                  {node.error && <p className="workflow-node-error">{node.error}</p>}
                  {hasOutput && (
                    <pre className="workflow-node-output">
                      {typeof node.output === "string"
                        ? node.output
                        : JSON.stringify(node.output, null, 2)}
                    </pre>
                  )}
                </div>
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function ApprovalCard({
  node,
  call,
  decide,
}: {
  node: WorkflowNodeRun;
  call: AgentToolCall | null;
  decide: (callId: string, decision: "approved" | "denied", remember: boolean) => Promise<void>;
}) {
  const [remember, setRemember] = useState(false);
  const [busy, setBusy] = useState(false);
  // A tool node's row points straight at its approval. An agent node's does
  // not: it parked through the agent loop, which wrote the record against the
  // backing run, so the caller resolves it that way and hands it in here.
  const callId = node.agent_tool_call_id ?? call?.id ?? "";
  const tool = call?.name || node.tool_name || node.node_key;

  async function choose(decision: "approved" | "denied") {
    if (!callId || busy) return;
    setBusy(true);
    try {
      await decide(callId, decision, remember);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="workflow-approval">
      <p className="workflow-approval-lead">
        <ShieldQuestion size={14} />
        This step is waiting for a person. Nothing has been written yet.
      </p>
      {/* The same markup the chat card and the document panel use, so a
          proposal reads identically wherever a person meets it. */}
      {call?.proposal_preview && (
        <div className="workflow-approval-preview">
          <ProposalDiff preview={call.proposal_preview} />
        </div>
      )}
      {callId ? (
        <>
          <label className="remember">
            <input
              type="checkbox"
              checked={remember}
              onChange={(event) => setRemember(event.target.checked)}
            />
            {/* The server grants at the scope of the card that raised it, and
                this card was raised by an automation running unattended. */}
            Always allow {tool} in workflows
          </label>
          <div className="workflow-approval-actions">
            <button
              className="ghost-button"
              disabled={busy}
              onClick={() => void choose("denied")}
            >
              <CircleSlash size={14} /> Deny
            </button>
            <button
              className="primary-button"
              disabled={busy}
              onClick={() => void choose("approved")}
            >
              <Check size={14} /> Approve
            </button>
          </div>
        </>
      ) : (
        <p className="workflow-node-meta">
          The approval record for this step is older than the last fifty and
          could not be loaded. It is still decidable from the conversation this
          run created.
        </p>
      )}
    </div>
  );
}

/**
 * The card a manual node shows while the run is paused for a person.
 *
 * A manual node is not asking about a write somebody is about to make — it is
 * the write, in the sense that its answer *is* the node's output. So this shows
 * the author's question and, when the node declares fields, the same input form
 * a run starts from, then turns Proceed into an approval that carries the typed
 * values and Reject into a denial that cancels the run.
 *
 * The form owns its own validation (`WorkflowInputForm` over the node's
 * fields), and Proceed is its submit button by `form={formId}` — so a value the
 * browser can see is wrong is caught before the decision is ever sent, exactly
 * as the run form catches it before a run is started. With no fields there is
 * nothing to type, so Proceed sends an empty object outright.
 */
function ManualNodeCard({
  node,
  run,
  call,
  decide,
}: {
  node: WorkflowGraphNode;
  run: WorkflowNodeRun;
  call: AgentToolCall | null;
  decide: (
    callId: string,
    decision: "approved" | "denied",
    remember: boolean,
    inputs?: Record<string, unknown>,
  ) => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const formId = useId();
  // Mirrors ApprovalCard: the node parks its own call and stores the id, but a
  // record aged out of the last fifty leaves only the backing conversation.
  const callId = run.agent_tool_call_id ?? call?.id ?? "";
  const fields = node.fields ?? [];

  async function settle(
    decision: "approved" | "denied",
    inputs?: Record<string, unknown>,
  ) {
    if (!callId || busy) return;
    setBusy(true);
    try {
      await decide(callId, decision, false, inputs);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="workflow-approval">
      <p className="workflow-approval-lead">
        <UserRound size={14} />
        {node.prompt || "This step is waiting for a person to proceed or reject."}
      </p>
      {callId ? (
        <>
          {fields.length > 0 && (
            <WorkflowInputForm
              formId={formId}
              specs={fields}
              remoteProblems={[]}
              onRun={(payload) => void settle("approved", payload)}
              disabled={busy}
            />
          )}
          <div className="workflow-approval-actions">
            <button
              className="ghost-button"
              disabled={busy}
              onClick={() => void settle("denied")}
            >
              <CircleSlash size={14} /> Reject
            </button>
            {fields.length > 0 ? (
              <button className="primary-button" type="submit" form={formId} disabled={busy}>
                <Check size={14} /> Proceed
              </button>
            ) : (
              <button
                className="primary-button"
                disabled={busy}
                onClick={() => void settle("approved", {})}
              >
                <Check size={14} /> Proceed
              </button>
            )}
          </div>
        </>
      ) : (
        <p className="workflow-node-meta">
          The approval record for this step is older than the last fifty and
          could not be loaded. It is still decidable from the conversation this
          run created.
        </p>
      )}
    </div>
  );
}

export function WorkflowsView({
  setError,
  composeRequested = false,
  onComposeHandled,
  onWorkspaceChanged,
}: WorkflowsViewProps) {
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [runsByWorkflow, setRunsByWorkflow] = useState<RunMap>({});
  const [activeId, setActiveId] = useState("");
  const [runDetail, setRunDetail] = useState<WorkflowRunDetail | null>(null);
  const [approvals, setApprovals] = useState<Record<string, AgentToolCall>>({});
  const [composing, setComposing] = useState(composeRequested);
  const [schedulingEnabled, setSchedulingEnabled] = useState<boolean | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  /** The API's last refusal of a run payload, one entry per offending input. */
  const [inputProblems, setInputProblems] = useState<InputProblem[]>([]);

  const active = workflows.find((item) => item.id === activeId) ?? null;
  const runs = runsByWorkflow[activeId] ?? [];

  /**
   * The enabled agents, for each agent step's "runs as" picker. Fetched once;
   * a failure leaves the roster empty and the canvas read-only, which is the
   * graph exactly as it was before agents were assignable.
   */
  const [agentRoster, setAgentRoster] = useState<AgentInfo[]>([]);
  useEffect(() => {
    let cancelled = false;
    void api
      .listAgents()
      .then((rows) => {
        if (!cancelled) setAgentRoster(rows.filter((row) => row.enabled));
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  async function assignAgent(nodeId: string, agentId: string) {
    if (!active) return;
    const graph = {
      ...active.graph,
      nodes: (active.graph.nodes ?? []).map((node) =>
        node.id === nodeId ? { ...node, agent: agentId } : node,
      ),
    };
    setBusy(true);
    try {
      // The server re-validates (and bumps the version): a pick that names a
      // vanished agent is rejected with a compile error, never stored.
      const updated = await api.updateWorkflow(active.id, { graph });
      setWorkflows((rows) =>
        rows.map((row) => (row.id === updated.id ? updated : row)),
      );
    } catch (caught) {
      setError(describeError(caught, "Could not assign the agent"));
    } finally {
      setBusy(false);
    }
  }

  /**
   * Backing runs this workspace's ceiling is currently holding, by run id.
   *
   * The last of the three signals `resolvePausedReason` weighs, and the only
   * one that costs a request: it is read for a run that reported no
   * `paused_reason` at all — an older API, or a row from before the column.
   *
   * Rather than infer a budget park from the *absence* of an approval — an
   * absence that also means "the record is older than the last fifty", which is
   * a different sentence and a different next step — this asks the one endpoint
   * that reports the truth: `GET /api/admin/budget` lists the runs the ceiling
   * is holding, by id. It is owner-only, so a member falls through to the
   * approval card exactly as before; and when the reason does arrive with the
   * run, the branch above answers and this request is never made.
   */
  const [heldByBudget, setHeldByBudget] = useState<Set<string>>(new Set());
  /** Runs already asked about while parked, so the poll asks once, not always. */
  const askedHeld = useRef(new Set<string>());
  const readHeld = useCallback(async () => {
    try {
      const budget = await api.getAdminBudget();
      setHeldByBudget(new Set(budget.runs_parked_on_budget.map((run) => run.run_id)));
    } catch {
      // 403 for a member, or the ceiling is unreachable. Either way there is
      // nothing extra to say, and the approval path is unchanged.
    }
  }, []);

  /**
   * Is there a call waiting on a decision against this backing run?
   *
   * The signal `resolvePausedReason` believes first: `_park_for_budget` writes no
   * `AgentToolCall` at all, so a run that has a proposed one is parked on a
   * person, whatever a field or a list said a moment ago. The map is replaced
   * wholesale on every poll of a parked run, so it cannot go stale the way the
   * two answers it outranks can.
   */
  const hasProposal = useCallback(
    (runId: string | null): boolean =>
      Boolean(runId) &&
      Object.values(approvals).some(
        (call) => call.run_id === runId && call.status === "proposed",
      ),
    [approvals],
  );

  /** What stopped this run. The precedence lives in `workflow-format`. */
  const pausedReasonOf = useCallback(
    (run: Pick<WorkflowRun, "status" | "paused_reason" | "run_id">): string =>
      resolvePausedReason(run, {
        proposed: hasProposal(run.run_id),
        heldByBudget: Boolean(run.run_id) && heldByBudget.has(run.run_id!),
      }),
    [heldByBudget, hasProposal],
  );

  const load = useCallback(async () => {
    try {
      const rows = await api.listWorkflows();
      setWorkflows(rows);
      const scanned = await Promise.all(
        rows.slice(0, INBOX_WORKFLOWS).map(async (workflow) => {
          try {
            return [
              workflow.id,
              await api.listWorkflowRuns(workflow.id, RUNS_PER_WORKFLOW),
            ] as const;
          } catch {
            // One workflow's history failing must not blank the list.
            return [workflow.id, [] as WorkflowRun[]] as const;
          }
        }),
      );
      setRunsByWorkflow(Object.fromEntries(scanned));
      // One question for the whole inbox: is anything in it held by the
      // ceiling rather than waiting for a decision? Asked only when something
      // is stopped and did not say why.
      if (
        scanned.some(([, runs]) =>
          runs.some((run) => run.status === "waiting_for_approval" && !run.paused_reason),
        )
      ) {
        await readHeld();
      }
    } catch (caught) {
      setError(describeError(caught, "Could not load workflows"));
    } finally {
      setLoaded(true);
    }
  }, [setError, readHeld]);

  useEffect(() => {
    void load();
  }, [load]);

  /**
   * Whether a stored cron can actually fire, asked of the ticker itself and
   * asked only once, and only when this workspace has something scheduled.
   *
   * The question costs a request that is *meant* to fail — 503 when nothing
   * dispatches, 401 when something does — so a workspace whose automations are
   * all manual never asks it. An unreachable answer stays `null`, which
   * `describeSchedule` reads as "we do not know" and never as "yes".
   */
  const hasSchedule = workflows.some((item) => item.trigger_kind === "schedule");
  const probed = useRef(false);
  useEffect(() => {
    if (!hasSchedule || probed.current) return;
    probed.current = true;
    void api
      .workflowSchedulingEnabled()
      .then(setSchedulingEnabled)
      .catch(() => undefined);
  }, [hasSchedule]);

  useEffect(() => {
    if (!composeRequested) return;
    onComposeHandled?.();
    setComposing(true);
    setActiveId("");
    // The request flag is the trigger; the callback is stable enough.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [composeRequested]);

  // Held in refs so `openRun` can stay a stable callback: it is the body of a
  // polling interval, and a new identity every render would tear the interval
  // down and rebuild it 1.2 seconds at a time.
  const settled = useRef(new Set<string>());
  const changed = useRef(onWorkspaceChanged);
  useEffect(() => {
    changed.current = onWorkspaceChanged;
  }, [onWorkspaceChanged]);

  const refreshRuns = useCallback(async (workflowId: string) => {
    const rows = await api.listWorkflowRuns(workflowId, RUNS_PER_WORKFLOW);
    setRunsByWorkflow((current) => ({ ...current, [workflowId]: rows }));
  }, []);

  const openRun = useCallback(
    async (workflowRunId: string) => {
      try {
        const detail = await api.getWorkflowRun(workflowRunId);
        setRunDetail(detail);
        // Parked nodes are fetched by state, not only by link: an agent node
        // that parked has no `agent_tool_call_id` of its own, and looking only
        // for the link is what made a waiting run undecidable.
        if (
          detail.nodes.some(
            (node) =>
              node.agent_tool_call_id || node.status === "waiting_for_approval",
          )
        ) {
          const calls = await api.listAgentToolCalls();
          setApprovals(Object.fromEntries(calls.map((call) => [call.id, call])));
        }
        // Asked only for a run that is stopped and did not say why — never on
        // the happy path, and never for a run whose reason arrived with it.
        //
        // Once per park, not once per poll: this function *is* the body of a
        // 1.2-second interval, and an approval park has an empty reason too
        // (the same serializer drops it), so an unguarded call here would put
        // a request a second behind every open parked run. The memo is cleared
        // when the run moves on, so a second park asks again.
        if (detail.status === "waiting_for_approval" && !detail.paused_reason) {
          if (!askedHeld.current.has(detail.id)) {
            askedHeld.current.add(detail.id);
            await readHeld();
          }
        } else {
          askedHeld.current.delete(detail.id);
        }
        // A finished run is where its writes have actually landed — the
        // decision only authorised them. Told once per run, so the shell's
        // lists catch up without a poll turning into a refresh storm.
        if (runIsSettled(detail.status) && !settled.current.has(detail.id)) {
          settled.current.add(detail.id);
          changed.current?.();
          // And the list beside it, once, for the same reason. The interval
          // that keeps that list current is torn down by this very status
          // change, so the last row it wrote is the second-to-last state the
          // run was in — a run reading "Succeeded" in the banner and "Queued"
          // in the list two inches away, indefinitely.
          void refreshRuns(detail.workflow_id).catch(() => undefined);
        }
      } catch (caught) {
        setError(describeError(caught, "Could not open that run"));
      }
    },
    [setError, readHeld, refreshRuns],
  );

  // A run that has not settled is a run worth re-reading: nodes go from running
  // to parked to done while nobody clicks anything.
  const watchedRun = runDetail && !runIsSettled(runDetail.status) ? runDetail.id : "";
  const watchedWorkflow = runDetail?.workflow_id ?? "";
  useEffect(() => {
    if (!watchedRun) return;
    const timer = window.setInterval(() => {
      void openRun(watchedRun);
      if (watchedWorkflow) void refreshRuns(watchedWorkflow).catch(() => undefined);
    }, 1200);
    return () => window.clearInterval(timer);
  }, [watchedRun, watchedWorkflow, openRun, refreshRuns]);

  function select(workflow: Workflow) {
    setComposing(false);
    setActiveId(workflow.id);
    setRunDetail(null);
    setInputProblems([]);
  }

  async function saved(workflow: Workflow) {
    setWorkflows((rows) => [workflow, ...rows.filter((row) => row.id !== workflow.id)]);
    setRunsByWorkflow((current) => ({ ...current, [workflow.id]: [] }));
    setComposing(false);
    setActiveId(workflow.id);
    setRunDetail(null);
    setInputProblems([]);
  }

  async function setStatus(workflow: Workflow, status: WorkflowStatus) {
    setBusy(true);
    try {
      const updated = await api.updateWorkflow(workflow.id, { status });
      setWorkflows((rows) => rows.map((row) => (row.id === updated.id ? updated : row)));
    } catch (caught) {
      setError(describeError(caught, "Could not change that workflow"));
    } finally {
      setBusy(false);
    }
  }

  async function run(workflow: Workflow, payload: Record<string, unknown>) {
    setBusy(true);
    setInputProblems([]);
    try {
      const started = await api.runWorkflow(workflow.id, payload);
      setRunsByWorkflow((current) => ({
        ...current,
        [workflow.id]: [started, ...(current[workflow.id] ?? [])],
      }));
      await openRun(started.id);
    } catch (caught) {
      // The refusal a form can answer: the graph is fine, a field is not, and
      // the server named which. Anything else is still an error banner.
      if (caught instanceof WorkflowInputError) {
        setInputProblems(
          attributeProblems(caught.problems, workflow.graph.inputs ?? []),
        );
      } else {
        setError(describeError(caught, "Could not start that run"));
      }
    } finally {
      setBusy(false);
    }
  }

  async function remove(workflow: Workflow) {
    if (!window.confirm(`Delete “${workflow.name}” and its run history?`)) return;
    try {
      await api.deleteWorkflow(workflow.id);
      setWorkflows((rows) => rows.filter((row) => row.id !== workflow.id));
      setRunsByWorkflow((current) => {
        const next = { ...current };
        delete next[workflow.id];
        return next;
      });
      if (activeId === workflow.id) {
        setActiveId("");
        setRunDetail(null);
      }
    } catch (caught) {
      setError(describeError(caught, "Could not delete that workflow"));
    }
  }

  async function decide(
    callId: string,
    decision: "approved" | "denied",
    remember: boolean,
    inputs?: Record<string, unknown>,
  ) {
    try {
      await api.decideAgentToolCall(callId, decision, remember, { inputs });
      if (runDetail) await openRun(runDetail.id);
      if (watchedWorkflow) await refreshRuns(watchedWorkflow);
      onWorkspaceChanged?.();
    } catch (caught) {
      setError(describeError(caught, "Could not record that decision"));
    }
  }

  // Every run, across every workflow scanned, that stopped and is waiting for a
  // person. The reason this view exists rather than only a per-workflow page.
  const waiting = workflows.flatMap((workflow) =>
    (runsByWorkflow[workflow.id] ?? [])
      .filter((item) => item.status === "waiting_for_approval")
      .map((item) => ({ workflow, run: item })),
  );

  const nodeRuns = Object.fromEntries(
    (runDetail?.nodes ?? []).map((node) => [node.node_key, node]),
  );

  /**
   * The approval a parked node is waiting on.
   *
   * Two shapes, because there are two ways to park. A **tool** node writes the
   * `AgentToolCall` itself and stores its id. An **agent** node parks inside
   * the chat loop, which writes the record against this workflow's backing
   * `Run` and never touches the node row — so that one is found by the run.
   */
  function approvalFor(node: WorkflowNodeRun): AgentToolCall | null {
    if (node.agent_tool_call_id) return approvals[node.agent_tool_call_id] ?? null;
    if (!runDetail?.run_id) return null;
    return (
      Object.values(approvals).find(
        (call) => call.run_id === runDetail.run_id && call.status === "proposed",
      ) ?? null
    );
  }
  // One question asked once: is the open run stopped on money rather than on a
  // decision? Everything that renders differently for it reads this.
  const heldOnBudget = Boolean(
    runDetail && isBudgetPark(runDetail.status, pausedReasonOf(runDetail)),
  );
  const schedule = active ? describeSchedule(active, schedulingEnabled) : null;
  const reviewability = describeReviewability(
    active?.graph ?? { nodes: [] },
    active?.requires_approval ?? false,
  );

  return (
    <div className="workflow-layout">
      <aside className="workflow-sidebar">
        <div className="workflow-sidebar-head">
          <span>Workflows</span>
          <button
            className="icon-button"
            aria-label="New workflow"
            onClick={() => {
              setComposing(true);
              setActiveId("");
              setRunDetail(null);
            }}
          >
            <Plus size={16} />
          </button>
        </div>

        {waiting.length > 0 && (
          <div className="workflow-inbox">
            <span className="workflow-inbox-title">
              <ShieldQuestion size={13} />
              Waiting for you
            </span>
            {waiting.map(({ workflow, run: parked }) => (
              <button
                key={parked.id}
                className="workflow-inbox-item"
                onClick={() => {
                  select(workflow);
                  void openRun(parked.id);
                }}
              >
                <strong>{workflow.name}</strong>
                <span>
                  {/* The inbox holds two different stops. One wants your
                      decision; the other wants a bigger number, and calling
                      both "waiting for you" is what sends an owner looking for
                      a card that was never written. */}
                  {runStatusLabel(parked.status, pausedReasonOf(parked))} ·{" "}
                  {parked.trigger === "schedule" ? "Scheduled run" : "Manual run"} ·{" "}
                  {formatRelative(parked.created_at)}
                </span>
              </button>
            ))}
          </div>
        )}

        {workflows.length === 0 ? (
          <p className="workflow-empty">
            {loaded
              ? "No workflows yet."
              : "Loading…"}
          </p>
        ) : (
          <ul className="workflow-items">
            {workflows.map((workflow) => {
              const history = runsByWorkflow[workflow.id] ?? [];
              const latest = history[0];
              return (
                <li key={workflow.id}>
                  <button
                    className={
                      workflow.id === activeId ? "workflow-item active" : "workflow-item"
                    }
                    onClick={() => select(workflow)}
                  >
                    <WorkflowIcon size={14} />
                    <span className="workflow-item-name">{workflow.name}</span>
                    <StatusChip status={workflow.status} />
                  </button>
                  <div className="workflow-item-meta">
                    <span>
                      {workflow.trigger_kind === "schedule" ? "Scheduled" : "Manual"}
                    </span>
                    {latest && (
                      <span>{runStatusLabel(latest.status, pausedReasonOf(latest))}</span>
                    )}
                    {latest && <span>{formatRelative(latest.created_at)}</span>}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </aside>

      {composing && (
        <div className="workflow-main">
          <AuthorPanel setError={setError} onSaved={saved} />
        </div>
      )}

      {!composing && !active && (
        <div className="workflow-main empty">
          <div className="empty-state">
            <WorkflowIcon size={22} />
            <p>
              {loaded
                ? "Pick a workflow, or describe a new one in a sentence."
                : "Loading workflows…"}
            </p>
          </div>
        </div>
      )}

      {!composing && active && (
        <div className="workflow-main">
          <header className="workflow-head">
            <div>
              <h1>{active.name}</h1>
              <p>{active.description || active.source_prompt}</p>
            </div>
            <div className="workflow-head-actions">
              <button
                className="primary-button"
                type="submit"
                form={RUN_FORM_ID}
                disabled={busy || active.status === "disabled"}
              >
                <Play size={14} /> Run now
              </button>
              <button
                className="ghost-button"
                disabled={busy}
                onClick={() =>
                  void setStatus(active, active.status === "active" ? "disabled" : "active")
                }
              >
                {active.status === "active" ? "Disable" : "Activate"}
              </button>
              <button
                className="icon-button"
                aria-label={`Delete ${active.name}`}
                onClick={() => void remove(active)}
              >
                <Trash2 size={15} />
              </button>
            </div>
          </header>

          <div className="workflow-facts">
            <StatusChip status={active.status} />
            <span>version {active.version}</span>
          </div>

          {/* The two questions asked of an automation before it is trusted:
              what will it do, and when will it happen. Same shape, side by
              side, because neither answer is worth more than the other. */}
          <div className="workflow-notes">
            <div className={`workflow-note ${reviewability.tone}`}>
              <strong>{reviewability.headline}</strong>
              <span>{reviewability.text}</span>
            </div>
            {schedule && (
              <div className={`workflow-note ${schedule.tone}`}>
                <strong>{schedule.headline}</strong>
                <span>{schedule.detail}</span>
              </div>
            )}
          </div>

          {active.source_prompt && (
            <blockquote className="workflow-source">{active.source_prompt}</blockquote>
          )}

          {/* Remounted per workflow: the draft is that graph's declaration
              pre-filled with that graph's defaults, and carrying it across a
              selection would put one workflow's answers in another's boxes. */}
          <WorkflowInputForm
            key={active.id}
            formId={RUN_FORM_ID}
            specs={active.graph.inputs ?? []}
            remoteProblems={inputProblems}
            disabled={busy || active.status === "disabled"}
            onRun={(payload) => void run(active, payload)}
          />

          <div className="workflow-body">
            <section className="workflow-graph-pane">
              <div className="panel-title">
                <div>
                  <strong>{runDetail ? "This run" : "The graph"}</strong>
                </div>
                {runDetail && (
                  <button className="ghost-button" onClick={() => setRunDetail(null)}>
                    <X size={13} /> Back to the graph
                  </button>
                )}
              </div>
              {runDetail && (
                <div
                  className={`workflow-run-banner ${
                    heldOnBudget ? "budget" : runDetail.status
                  }`}
                >
                  <strong>
                    {runStatusLabel(runDetail.status, pausedReasonOf(runDetail))}
                  </strong>
                  <span>
                    {runDetail.trigger === "schedule"
                      ? "Started by the schedule, with nobody watching."
                      : "Started by hand."}{" "}
                    Version {runDetail.workflow_version} ·{" "}
                    {formatRelative(runDetail.created_at)}
                  </span>
                  {runDetail.error && (
                    <span className="workflow-run-error">{runDetail.error}</span>
                  )}
                </div>
              )}
              <WorkflowGraphView
                graph={active.graph}
                nodeRuns={runDetail ? nodeRuns : undefined}
                pausedReason={runDetail ? pausedReasonOf(runDetail) : ""}
                agents={agentRoster}
                onAssignAgent={(nodeId, agentId) => void assignAgent(nodeId, agentId)}
                renderApproval={(node) => {
                  if (node.status !== "waiting_for_approval") return null;
                  // Both parks land the node on the same status, and only the
                  // run says which. A budget park wrote no `AgentToolCall`, so
                  // the approval card would render its "the approval record is
                  // older than the last fifty" fallback — a sentence about a
                  // record that does not exist and never will.
                  if (heldOnBudget) {
                    return (
                      <BudgetHold
                        park={null}
                        menuId={`workflow-spend-ceiling-${node.node_key}`}
                        onReleased={() => void openRun(runDetail!.id)}
                      />
                    );
                  }
                  // A manual node parks on the same status as a write, but its
                  // decision is a person answering a question, not authorising
                  // a call — so it gets a card that asks the question and, if it
                  // declares fields, collects them. The graph, not the run row,
                  // is what says which kind of park this is.
                  const graphNode = active.graph.nodes?.find(
                    (candidate) => candidate.id === node.node_key,
                  );
                  if (graphNode?.kind === "manual") {
                    return (
                      <ManualNodeCard
                        node={graphNode}
                        run={node}
                        call={approvalFor(node)}
                        decide={decide}
                      />
                    );
                  }
                  return (
                    <ApprovalCard node={node} call={approvalFor(node)} decide={decide} />
                  );
                }}
              />
              {runDetail && (
                <WorkflowRunTimeline
                  run={runDetail}
                  pausedReason={pausedReasonOf(runDetail)}
                />
              )}
            </section>

            <section className="workflow-runs-pane">
              <div className="panel-title">
                <div>
                  <strong>Runs</strong>
                </div>
                <span className="panel-count">{runs.length}</span>
              </div>
              {runs.length === 0 ? (
                <p className="workflow-empty">
                  This workflow has not run yet.
                </p>
              ) : (
                <ul className="workflow-run-list">
                  {runs.map((item) => (
                    <li key={item.id}>
                      <button
                        className={
                          runDetail?.id === item.id
                            ? "workflow-run-item active"
                            : "workflow-run-item"
                        }
                        onClick={() => void openRun(item.id)}
                      >
                        <span
                          className={`workflow-run-status ${
                            isBudgetPark(item.status, pausedReasonOf(item))
                              ? "budget"
                              : item.status
                          }`}
                        >
                          {runStatusLabel(item.status, pausedReasonOf(item))}
                        </span>
                        <span className="workflow-run-when">
                          {item.trigger === "schedule" ? "Scheduled" : "Manual"} ·{" "}
                          {formatRelative(item.created_at)}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </div>
        </div>
      )}
    </div>
  );
}
