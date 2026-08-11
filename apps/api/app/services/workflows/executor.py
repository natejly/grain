"""The DAG executor ADR 0007 specified and deliberately did not build.

It is a topological walk, and everything interesting about it is what it *does
not* own. It has no state machine of its own, no checkpoint format, no second
notion of "parked". A node's tool call goes through `build_registry` ->
`resolve_policy` -> `execute_agent_tool_call`, which is the path a chat turn
takes, so there is exactly one execution path to audit and one approval gate to
get right. That is the whole argument of ADR 0007 expressed as code.

Four properties are load-bearing, and each has a test:

**Resume skips completed work.** `workflow_node_runs` is unique on
(workflow_run_id, node_key), so "which nodes finished" is a row set rather than a
convention. A resumed run reads the table and starts where the last one stopped.

**A node interrupted mid-write is not re-run.** The unique constraint proves a
*committed* node ran once. It says nothing about a node that was executing when
the process died, and for a tool that sends email "probably already happened" is
not a basis for doing it again. A read-only node is retried; a write-capable one
ends the run with `node_interrupted` and a human decides. ADR 0007 says at least
once is the wrong number, and this is where that costs something.

**An approval parks the workflow, not just the node.** The park writes the same
`AgentToolCall(status="proposed")` a chat turn writes, against the same `Run`,
and `POST /api/agent-tool-calls/{id}/decision` resumes it. One park, one resume,
one row. An unattended run with nobody present therefore waits rather than
proceeding, which is the containment the whole design rests on.

**One node failing does not corrupt the run.** Every failure path rolls back,
writes a terminal status and a legible error, and marks the nodes that never got
to run as `skipped`. The run record is always readable afterwards.

**Nothing is left on the floor, and nothing is picked up twice.** A run whose
process died is claimed by `recover_workflow_runs` under the same
`runs.run_lease_seconds` lease the chat path uses, and resumed through the same
`advance_run` — so recovery is the ordinary walk starting from the ordinary
place, not a second mechanism with its own opinions. Everything above still
holds while it does: a parked run is left alone because it is waiting on a
person, an interrupted write still refuses to be retried, and the retry a
read-only node does get is bounded so a node that kills its process cannot be
resumed forever.

The backing `Run` is created eagerly rather than lazily, which is a change of
emphasis from ADR 0007's "null until a node needs one". It buys two things worth
more than the row: `execute_agent_tool_call` needs a run to attach its
`AgentToolCall` and its events to, so reusing it at all requires one; and the
approval card a parked workflow produces then appears in a conversation, which
is the only approval inbox the product has.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, cast

import jsonschema
from jsonschema import Draft202012Validator
from sqlalchemy import or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from ...clock import utcnow
from ...config import Settings, get_settings
from ...database import SessionLocal
from ...models import (
    Agent,
    AgentToolCall,
    Conversation,
    Run,
    Workflow,
    WorkflowNodeRun,
    WorkflowRun,
)
from ..agent_loop import (
    WORKFLOW_SCOPE,
    Cancelled,
    Done,
    Outcome,
    Paused,
    describe_proposal,
    execute_agent_tool_call,
    resolve_policy,
    run_agent_turn,
)
from ..audit import record_audit
from ..events import append_event
from ..llm_tools import ToolContext, ToolSpec, build_registry
from . import refs
from .dag import NodeSpec, WorkflowGraph
from .validate import parse_graph, topological_order, validate_graph

logger = logging.getLogger(__name__)

#: Terminal for a workflow run. Mirrors `runs`' vocabulary on purpose — ADR 0007
#: shaped `workflow_runs` like `runs` so a reader holds one status model.
TERMINAL = {"succeeded", "failed", "cancelled"}

#: A workflow run in one of these is *waiting on a machine*, so a sweep may take
#: it. `waiting_for_approval` is deliberately absent: that run is waiting on a
#: person, and recovering it would resume a graph nobody has approved yet.
RECOVERABLE = ("queued", "running")

#: How much of a node's output travels into the next node's arguments. A tool
#: that returns a 10MB document must not turn the graph into a memory profile.
MAX_OUTPUT_CHARS = 100_000

#: How many times a process may die on the *same* node before the run is failed
#: instead of resumed. Without a bound, a node that reliably kills its process —
#: a tool that OOMs on a large input, a wedged MCP connection — is retried by
#: every sweep forever. `WorkflowNodeRun.attempt` already counts exactly this, so
#: the bound is a comparison rather than new state.
MAX_NODE_ATTEMPTS = 3

#: A run older than this is failed rather than recovered, whatever state it is
#: in. It is the bound of last resort: it catches the loop `MAX_NODE_ATTEMPTS`
#: cannot see, where the process dies in `_prepare` before any node row exists.
#: It is also the right *product* answer, and for the reason `schedule.CATCHUP`
#: gives — finishing yesterday's "post the daily summary" today is not a rescue.
RECOVERY_MAX_AGE = timedelta(hours=12)


class _Halt(Exception):
    """A node decided the run cannot continue. Carries the terminal status."""

    def __init__(self, status: str, code: str, message: str, node: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.node = node


# --------------------------------------------------------------------------
# Starting a run
# --------------------------------------------------------------------------


def start_run(
    db: Session,
    workflow: Workflow,
    *,
    user_id: str,
    trigger: str = "manual",
    payload: Optional[Mapping[str, Any]] = None,
) -> WorkflowRun:
    """Record one execution of *this version* of the workflow.

    `graph_json` is copied onto the run rather than joined to the workflow.
    A workflow edited on Tuesday must not rewrite what Monday's run executed,
    and a run parked over an edit must resume the graph it started.
    """
    workflow_run = WorkflowRun(
        workspace_id=workflow.workspace_id,
        workflow_id=workflow.id,
        created_by=user_id,
        workflow_version=workflow.version,
        graph_json=workflow.graph_json,
        trigger=trigger,
        status="queued",
        input_json=json.dumps(dict(payload or {}), default=str),
    )
    db.add(workflow_run)
    db.flush()
    record_audit(
        db,
        workspace_id=workflow.workspace_id,
        actor_id=user_id,
        action="workflow_run.started",
        resource_type="workflow_run",
        resource_id=workflow_run.id,
        detail={"workflow_id": workflow.id, "trigger": trigger},
    )
    return workflow_run


def process_workflow_run(workflow_run_id: str) -> None:
    """Background-task entry point. Owns its session, like `services/runs`."""
    db = SessionLocal()
    try:
        workflow_run = db.get(WorkflowRun, workflow_run_id)
        if workflow_run is None or workflow_run.status in TERMINAL:
            return
        advance_run(db, workflow_run)
    finally:
        db.close()


# --------------------------------------------------------------------------
# Recovery after an unclean stop
# --------------------------------------------------------------------------


def recover_workflow_runs() -> List[str]:
    """Resume every workflow run whose executing process died. Owns its session.

    The chat-side twin of this is `recover_durable_work`, and the shape is
    copied rather than reinvented: claim everything under one session, commit,
    close, then hand each claimed id to the ordinary entry point. Executing
    inside the claiming transaction would hold it open for the length of a graph.

    ADR 0007 left this out and said what the gap cost: "the run stays `running`
    until somebody re-runs it". For an automation nobody is watching, that is the
    whole difference between reliable and usually.
    """
    db = SessionLocal()
    try:
        claimed = claim_orphaned_runs(db)
    finally:
        db.close()
    for workflow_run_id in claimed:
        process_workflow_run(workflow_run_id)
    return claimed


def claim_orphaned_runs(
    db: Session,
    *,
    settings: Optional[Settings] = None,
    moment: Optional[datetime] = None,
) -> List[str]:
    """Take ownership of the runs a dead process left behind. Executes nothing.

    Split from `recover_workflow_runs` so a caller that already has a request's
    session — `POST /api/workflows/tick` — can claim synchronously and execute on
    a background task. Claiming is a few UPDATEs; executing is a graph.

    A run past `RECOVERY_MAX_AGE` is terminated here rather than returned, so the
    sweep that gives up on it also says so in the run record instead of leaving
    a row that quietly stops being considered.
    """
    settings = settings or get_settings()
    now = moment or utcnow()
    oldest = now - RECOVERY_MAX_AGE
    candidates = list(
        db.scalars(
            select(WorkflowRun).where(WorkflowRun.status.in_(RECOVERABLE))
        )
    )
    claimed: List[str] = []
    for workflow_run in candidates:
        if not _claim(db, workflow_run, now=now, settings=settings):
            continue
        if (workflow_run.started_at or workflow_run.created_at) < oldest:
            _terminate(
                db,
                workflow_run,
                _Halt(
                    "failed",
                    "run_abandoned",
                    "this run was interrupted more than "
                    f"{int(RECOVERY_MAX_AGE.total_seconds() // 3600)}h ago and is "
                    "too old to resume safely",
                ),
            )
            continue
        claimed.append(workflow_run.id)
    return claimed


def _claim(
    db: Session, workflow_run: WorkflowRun, *, now: datetime, settings: Settings
) -> bool:
    """Atomically become the one process working on this run. True means we won.

    Two API processes booting together sweep the same table in the same second,
    and the loser must find out *before* it starts a node rather than by
    colliding on the unique constraint half a tool call later. So the test is a
    conditional UPDATE and the database compares and swaps, exactly as
    `schedule.claim` does for the ticker.

    The lease is the one `runs.run_lease_seconds` already describes, held on the
    backing `Run` — the same row, the same column and the same expiry the chat
    path leases, because a workflow run *is* a chat run wearing a graph. A run
    that has no backing row yet has executed nothing, so there is no lease to
    take; the status column carries the claim instead, and the staleness window
    is what stops the sweep stealing a run whose `BackgroundTask` is about to
    start it two milliseconds from now.
    """
    lease = timedelta(seconds=settings.run_lease_seconds)
    if workflow_run.run_id:
        statement = (
            update(Run)
            .where(
                Run.id == workflow_run.run_id,
                or_(Run.lease_expires_at.is_(None), Run.lease_expires_at < now),
            )
            .values(lease_expires_at=now + lease)
        )
    else:
        statement = (
            update(WorkflowRun)
            .where(
                WorkflowRun.id == workflow_run.id,
                WorkflowRun.run_id.is_(None),
                WorkflowRun.status.in_(RECOVERABLE),
                WorkflowRun.updated_at < now - lease,
            )
            # `updated_at` is the claim's own expiry, so a process that dies
            # again between here and `_ensure_backing_run` becomes eligible one
            # lease later rather than never.
            .values(status="running", updated_at=now)
        )
    # `Session.execute` is typed as returning `Result`, which has no rowcount; a
    # DML statement always yields a `CursorResult`, which does.
    result = cast("CursorResult[Any]", db.execute(statement))
    db.commit()
    if result.rowcount != 1:
        return False
    db.refresh(workflow_run)
    return True


# --------------------------------------------------------------------------
# The walk
# --------------------------------------------------------------------------


def advance_run(
    db: Session,
    workflow_run: WorkflowRun,
    *,
    settings: Optional[Settings] = None,
    registry: Optional[Mapping[str, ToolSpec]] = None,
) -> WorkflowRun:
    """Execute from wherever this run left off. Never raises for node failure.

    Idempotent by construction: a node that already succeeded is read from
    `workflow_node_runs` rather than executed, so calling this after a crash,
    after an approval, or twice by accident does the same thing.

    `registry` is injectable only so a caller that already built one does not
    build it twice — never to widen it past what the workspace actually has.
    """
    if workflow_run.status in TERMINAL:
        return workflow_run
    settings = settings or get_settings()
    try:
        state = _prepare(db, workflow_run, settings=settings, registry=registry)
    except _Halt as halt:
        _terminate(db, workflow_run, halt)
        return workflow_run

    try:
        _walk(db, workflow_run, state)
    except _Halt as halt:
        _terminate(db, workflow_run, halt)
    except Exception as exc:  # noqa: BLE001 - one node must not corrupt the run
        logger.warning("workflow run %s failed", workflow_run.id, exc_info=True)
        _terminate(
            db,
            workflow_run,
            _Halt("failed", "executor_error", str(exc)[:1000]),
        )
    return workflow_run


class _State:
    """Everything one advance needs, resolved once."""

    def __init__(
        self,
        *,
        graph: WorkflowGraph,
        order: List[str],
        registry: Mapping[str, ToolSpec],
        context: ToolContext,
        run: Run,
        payload: Mapping[str, Any],
        settings: Settings,
    ) -> None:
        self.graph = graph
        self.order = order
        self.nodes: Dict[str, NodeSpec] = {node.id: node for node in graph.nodes}
        self.registry = registry
        self.context = context
        self.run = run
        self.payload = payload
        self.settings = settings
        self.outputs: Dict[str, Any] = {}


def _prepare(
    db: Session,
    workflow_run: WorkflowRun,
    *,
    settings: Settings,
    registry: Optional[Mapping[str, ToolSpec]],
) -> _State:
    graph, errors = parse_graph(_json_object(workflow_run.graph_json))
    if graph is None:
        raise _Halt(
            "failed",
            "graph_unreadable",
            "; ".join(item.render() for item in errors) or "graph is not a document",
        )

    run = _ensure_backing_run(db, workflow_run, graph)
    # Take the lease *before* the slow part, not after. `build_registry` talks to
    # every connected MCP server, so the gap between creating the backing run and
    # leasing it is seconds wide, and an unleased backing run is exactly what
    # `_claim` reads as "the process holding this is gone".
    run.lease_expires_at = utcnow() + timedelta(seconds=settings.run_lease_seconds)
    db.commit()

    context = ToolContext(
        workspace_id=workflow_run.workspace_id,
        user_id=run.created_by,
        conversation_id=run.conversation_id,
    )
    tools = registry if registry is not None else build_registry(db, context)

    # ADR 0007: a graph is validated against a registry that changes underneath
    # it. An MCP server disconnected since the compile takes its tools with it,
    # and the honest failure is loud and *before* the first node runs — half an
    # automation is worse than none of it.
    report = validate_graph(graph, tools)
    if not report.ok:
        raise _Halt(
            "failed",
            "graph_stale",
            "this graph no longer validates against the workspace: "
            + report.render(),
        )

    workflow_run.status = "running"
    workflow_run.started_at = workflow_run.started_at or utcnow()
    run.status = "running"
    run.lease_expires_at = utcnow() + timedelta(seconds=settings.run_lease_seconds)
    db.commit()

    return _State(
        graph=graph,
        order=topological_order(graph),
        registry=tools,
        context=context,
        run=run,
        payload=_json_object(workflow_run.input_json),
        settings=settings,
    )


def _walk(db: Session, workflow_run: WorkflowRun, state: _State) -> None:
    existing = _node_runs(db, workflow_run)
    for node_key in state.order:
        node = state.nodes[node_key]
        row = existing.get(node_key)

        if row is not None and row.status in {"succeeded", "skipped"}:
            state.outputs[node_key] = _stored_output(row)
            continue
        if row is not None and row.status == "waiting_for_approval":
            # Still parked. Somebody called advance twice; the decision endpoint
            # is what moves this forward.
            return
        if row is not None and row.status == "running":
            _reject_or_retry_interrupted(db, node, row, state)

        db.refresh(workflow_run)
        if workflow_run.cancel_requested:
            raise _Halt("cancelled", "cancelled", "the run was cancelled", node_key)

        # Renew before each node, not once per advance. A ten-node graph outlives
        # a 90-second lease easily, and an expired lease on a run that is very
        # much alive is an invitation for the next recovery sweep to claim it.
        state.run.lease_expires_at = utcnow() + timedelta(
            seconds=state.settings.run_lease_seconds
        )
        row = _begin_node(db, workflow_run, node, row)
        parked = _execute_node(db, workflow_run, node, row, state)
        if parked:
            return
        state.outputs[node_key] = _stored_output(row)

    _succeed(db, workflow_run, state)


def _reject_or_retry_interrupted(
    db: Session, node: NodeSpec, row: WorkflowNodeRun, state: _State
) -> None:
    """A node found mid-flight: retry it, or refuse to guess.

    `running` means a previous process died between "about to call the tool" and
    "the tool returned". Re-running is safe when the tool only reads. When it
    writes, the truthful statement is that nobody knows whether the email went
    out, and the useful behaviour is to say so rather than to send a second one.

    Read-only does not mean free, which is why the retry is bounded. A node that
    kills its process every time it runs would otherwise be resumed by every
    sweep for as long as the deployment lives.
    """
    if row.attempt >= MAX_NODE_ATTEMPTS:
        raise _Halt(
            "failed",
            "node_retry_exhausted",
            f"“{node.id}” was interrupted {row.attempt} times; it is not being "
            "retried again",
            node.id,
        )
    spec = state.registry.get(node.tool) if node.kind == "tool" else None
    read_only = spec is not None and spec.read_only
    if node.kind == "tool" and not read_only:
        raise _Halt(
            "failed",
            "node_interrupted",
            f"“{node.id}” was interrupted while calling a write-capable tool; "
            "whether it took effect is unknown, so it will not be retried "
            "automatically",
            node.id,
        )
    if node.kind == "agent":
        raise _Halt(
            "failed",
            "node_interrupted",
            f"“{node.id}” was interrupted mid-turn; an agent node chooses its "
            "own tools, so what it already did is unknown",
            node.id,
        )
    row.attempt += 1
    db.commit()


def _begin_node(
    db: Session,
    workflow_run: WorkflowRun,
    node: NodeSpec,
    row: Optional[WorkflowNodeRun],
) -> WorkflowNodeRun:
    if row is None:
        row = WorkflowNodeRun(
            workspace_id=workflow_run.workspace_id,
            workflow_run_id=workflow_run.id,
            node_key=node.id,
            kind=node.kind,
            tool_name=node.tool,
        )
        db.add(row)
    row.status = "running"
    row.started_at = utcnow()
    db.commit()
    return row


def _execute_node(
    db: Session,
    workflow_run: WorkflowRun,
    node: NodeSpec,
    row: WorkflowNodeRun,
    state: _State,
) -> bool:
    """Run one node. True means the run parked and this advance is over."""
    if node.kind == "agent":
        return _execute_agent_node(db, workflow_run, node, row, state)
    return _execute_tool_node(db, workflow_run, node, row, state)


# --------------------------------------------------------------------------
# Tool nodes
# --------------------------------------------------------------------------


def _execute_tool_node(
    db: Session,
    workflow_run: WorkflowRun,
    node: NodeSpec,
    row: WorkflowNodeRun,
    state: _State,
) -> bool:
    spec = state.registry.get(node.tool)
    if spec is None:  # pragma: no cover - _prepare re-validates first
        raise _Halt("failed", "tool_unknown", f"no tool named “{node.tool}”", node.id)

    arguments = _resolved_arguments(node, spec, state)
    row.arguments_json = json.dumps(arguments, default=str)[:MAX_OUTPUT_CHARS]

    policy = resolve_policy(
        db,
        workspace_id=workflow_run.workspace_id,
        spec=spec,
        # The whole point of the scope split. A standing `allow` granted in a
        # conversation is not an answer to "may this run unattended at 3am".
        scope=WORKFLOW_SCOPE,
    )
    row.policy = policy
    db.commit()

    if policy == "deny":
        raise _Halt(
            "failed",
            "policy_denied",
            f"this workspace denies “{spec.name}” to workflows",
            node.id,
        )
    if policy == "ask":
        _park(db, workflow_run, node, row, spec, state, arguments)
        return True

    _run_tool(db, workflow_run, node, row, spec, state, arguments, tool_call_id=None)
    return False


def _resolved_arguments(
    node: NodeSpec, spec: ToolSpec, state: _State
) -> Dict[str, Any]:
    """Substitute upstream outputs, then re-check against the tool's schema.

    ADR 0007's last unbuilt piece: a whole-value `{{ reference }}` could not be
    typed at compile time, so it is typed *here* — and rejected, never coerced.
    Coercion is how `{{ count.output }}` becomes the string "5" in an integer
    field and the tool does something subtly wrong instead of nothing at all.
    """
    try:
        resolved = refs.resolve(
            dict(node.arguments), outputs=state.outputs, payload=state.payload
        )
    except refs.ReferenceResolutionError as exc:
        raise _Halt("failed", "reference_unresolved", str(exc), node.id) from exc

    schema = spec.parameters
    if isinstance(schema, dict):
        try:
            Draft202012Validator.check_schema(schema)
        except jsonschema.exceptions.SchemaError:
            # The compiler already warned that this tool publishes an unusable
            # schema; refusing to run it now would punish the author for an MCP
            # server's bug.
            return dict(resolved)
        failure = next(iter(Draft202012Validator(schema).iter_errors(resolved)), None)
        if failure is not None:
            location = "/".join(str(part) for part in failure.absolute_path)
            where = f"arguments.{location}" if location else "arguments"
            raise _Halt(
                "failed",
                "arguments_invalid",
                f"{where} does not match the schema for “{spec.name}” once "
                f"upstream values were substituted: {failure.message}",
                node.id,
            )
    return dict(resolved)


def _run_tool(
    db: Session,
    workflow_run: WorkflowRun,
    node: NodeSpec,
    row: WorkflowNodeRun,
    spec: ToolSpec,
    state: _State,
    arguments: Mapping[str, Any],
    *,
    tool_call_id: Optional[str],
) -> None:
    """Execute through the chat loop's own executor and record the outcome."""
    raw_arguments = json.dumps(arguments, default=str)
    if tool_call_id is None:
        # Written before the call so the link survives a crash: the AgentToolCall
        # is the durable record of what was attempted, and a node row pointing at
        # nothing is a node nobody can investigate.
        record = AgentToolCall(
            workspace_id=workflow_run.workspace_id,
            run_id=state.run.id,
            name=spec.name,
            arguments_json=raw_arguments[:4000],
            status="running",
        )
        db.add(record)
        db.flush()
        tool_call_id = record.id
        row.agent_tool_call_id = tool_call_id
        db.commit()

    started = utcnow()
    result = execute_agent_tool_call(
        db,
        state.run,
        dict(state.registry),
        state.context,
        spec.name,
        raw_arguments,
        existing_id=tool_call_id,
    )
    # The verdict is read back off the row rather than sniffed out of the result
    # text: `execute_agent_tool_call` renders a tool's exception into a
    # model-readable string, and a node that decided "failed" by matching on
    # "Error:" would also fail on a tool that legitimately returned that word.
    outcome = db.get(AgentToolCall, tool_call_id)
    row.latency_ms = int((utcnow() - started).total_seconds() * 1000)
    if outcome is not None and outcome.status == "failed":
        row.error = outcome.error[:1000]
        row.status = "failed"
        db.commit()
        raise _Halt("failed", "node_failed", outcome.error or "the tool failed", node.id)

    _finish_node(db, row, result.content)


def _park(
    db: Session,
    workflow_run: WorkflowRun,
    node: NodeSpec,
    row: WorkflowNodeRun,
    spec: ToolSpec,
    state: _State,
    arguments: Mapping[str, Any],
) -> None:
    """Suspend the run on an approval, exactly as `agent_loop` suspends a turn.

    The row written here is an ordinary `AgentToolCall(status="proposed")` on an
    ordinary `Run` in `waiting_for_approval`, so the approval inbox, the audit
    trail and `POST /api/agent-tool-calls/{id}/decision` all work unchanged. The
    backing run's `agent_state_json` stays empty, and that absence is what tells
    the resume path a workflow node parked rather than a model turn.
    """
    raw_arguments = json.dumps(arguments, default=str)
    record = AgentToolCall(
        workspace_id=workflow_run.workspace_id,
        run_id=state.run.id,
        name=spec.name,
        arguments_json=raw_arguments[:4000],
        proposal_preview=describe_proposal(db, state.context, spec, raw_arguments),
        status="proposed",
    )
    db.add(record)
    db.flush()
    row.agent_tool_call_id = record.id
    row.status = "waiting_for_approval"
    workflow_run.status = "waiting_for_approval"
    state.run.status = "waiting_for_approval"
    state.run.lease_expires_at = None
    append_event(
        db,
        workspace_id=workflow_run.workspace_id,
        run_id=state.run.id,
        event_type="tool.proposed",
        payload={
            "tool_call_id": record.id,
            "tool_name": record.name,
            "description": spec.description,
            "arguments": record.arguments_json,
            "preview": record.proposal_preview,
            "workflow_run_id": workflow_run.id,
            "node_key": node.id,
        },
    )
    append_event(
        db,
        workspace_id=workflow_run.workspace_id,
        run_id=state.run.id,
        event_type="run.waiting_for_approval",
        payload={"status": "waiting_for_approval"},
    )
    record_audit(
        db,
        workspace_id=workflow_run.workspace_id,
        actor_id=workflow_run.created_by,
        action="agent_tool.proposed",
        resource_type="agent_tool_call",
        resource_id=record.id,
        detail={
            "tool": record.name,
            "workflow_run_id": workflow_run.id,
            "node_key": node.id,
            # The fact that decides how carefully this card should be read.
            "trigger": workflow_run.trigger,
        },
    )
    db.commit()


# --------------------------------------------------------------------------
# Agent nodes
# --------------------------------------------------------------------------


def _execute_agent_node(
    db: Session,
    workflow_run: WorkflowRun,
    node: NodeSpec,
    row: WorkflowNodeRun,
    state: _State,
) -> bool:
    """Hand one step to the chat agent loop, on this run's backing `Run`.

    Every tool the agent picks is gated by `resolve_policy`, and
    `policy_scope_for_run` resolves this run to `workflow` scope — so an agent
    node that reads a poisoned document and decides to email somebody parks,
    even in a workspace that clicked "always allow" on the mail tool in chat.
    That is ADR 0007's injection scenario, contained.
    """
    try:
        prompt = refs.resolve(
            node.prompt, outputs=state.outputs, payload=state.payload
        )
    except refs.ReferenceResolutionError as exc:
        raise _Halt("failed", "reference_unresolved", str(exc), node.id) from exc

    row.arguments_json = json.dumps({"prompt": prompt}, default=str)[:MAX_OUTPUT_CHARS]
    row.policy = "agent"
    state.run.prompt = str(prompt)
    db.commit()

    result = run_agent_turn(
        db,
        state.run,
        evidence=[],
        settings=state.settings,
        workflow_node=True,
    )
    if result is None:
        # The turn parked or was cancelled; `agent_loop` already wrote the
        # approval row and the run's status. Mirror it onto the workflow.
        db.refresh(state.run)
        return _mirror_agent_pause(db, workflow_run, row, state)
    _finish_node(db, row, result.answer)
    return False


def _mirror_agent_pause(
    db: Session, workflow_run: WorkflowRun, row: WorkflowNodeRun, state: _State
) -> bool:
    if state.run.status == "cancelled":
        raise _Halt(
            "cancelled", "cancelled", "the run was cancelled", row.node_key
        )
    row.status = "waiting_for_approval"
    workflow_run.status = "waiting_for_approval"
    db.commit()
    return True


# --------------------------------------------------------------------------
# Resumption, through the existing decision endpoint
# --------------------------------------------------------------------------


def resume_after_decision(
    db: Session,
    workflow_run: WorkflowRun,
    *,
    tool_call_id: str,
    decision: str,
    settings: Optional[Settings] = None,
) -> WorkflowRun:
    """A human decided on a call a *tool* node parked on. Called by `agent_loop`.

    Approval executes the call and the walk continues; denial ends the run. A
    denial is not a failure of the automation — it is the gate doing its job —
    so the run lands in `cancelled` with the node that was refused named in the
    error, which reads correctly in a run list.
    """
    settings = settings or get_settings()
    row = db.scalar(
        select(WorkflowNodeRun).where(
            WorkflowNodeRun.workflow_run_id == workflow_run.id,
            WorkflowNodeRun.agent_tool_call_id == tool_call_id,
        )
    )
    if row is None or row.status != "waiting_for_approval":
        return workflow_run
    if decision != "approved":
        row.status = "failed"
        row.error = "the approver denied this call"
        db.commit()
        _terminate(
            db,
            workflow_run,
            _Halt(
                "cancelled",
                "approval_denied",
                f"“{row.node_key}” was denied by the approver",
                row.node_key,
            ),
        )
        return workflow_run

    try:
        state = _prepare(db, workflow_run, settings=settings, registry=None)
        node = state.nodes.get(row.node_key)
        spec = state.registry.get(node.tool) if node is not None else None
        if node is None or spec is None:
            raise _Halt(
                "failed",
                "graph_stale",
                f"“{row.node_key}” is no longer part of this graph",
                row.node_key,
            )
        state.outputs = _completed_outputs(db, workflow_run)
        row.status = "running"
        db.commit()
        _run_tool(
            db,
            workflow_run,
            node,
            row,
            spec,
            state,
            json.loads(row.arguments_json or "{}"),
            tool_call_id=tool_call_id,
        )
    except _Halt as halt:
        _terminate(db, workflow_run, halt)
        return workflow_run
    except Exception as exc:  # noqa: BLE001
        logger.warning("workflow run %s failed on resume", workflow_run.id, exc_info=True)
        _terminate(db, workflow_run, _Halt("failed", "executor_error", str(exc)[:1000]))
        return workflow_run
    return advance_run(db, workflow_run, settings=settings)


def resume_after_agent_turn(
    db: Session,
    workflow_run: WorkflowRun,
    *,
    outcome: Outcome,
    settings: Optional[Settings] = None,
) -> WorkflowRun:
    """An `agent` node's parked turn finished. Called by `agent_loop`.

    The turn is over; the graph is not. `Paused` means the agent's *next* call
    also needs approval, so the run stays parked and the same endpoint will
    arrive again.
    """
    row = db.scalar(
        select(WorkflowNodeRun).where(
            WorkflowNodeRun.workflow_run_id == workflow_run.id,
            WorkflowNodeRun.status == "waiting_for_approval",
            WorkflowNodeRun.kind == "agent",
        )
    )
    if row is None:
        return workflow_run
    if isinstance(outcome, Paused):
        return workflow_run
    if isinstance(outcome, Cancelled):
        _terminate(
            db,
            workflow_run,
            _Halt("cancelled", "cancelled", "the run was cancelled", row.node_key),
        )
        return workflow_run
    assert isinstance(outcome, Done)
    _finish_node(db, row, outcome.answer)
    return advance_run(db, workflow_run, settings=settings)


# --------------------------------------------------------------------------
# Node and run bookkeeping
# --------------------------------------------------------------------------


def _finish_node(db: Session, row: WorkflowNodeRun, output: str) -> None:
    row.output_json = json.dumps(output[:MAX_OUTPUT_CHARS], default=str)
    row.status = "succeeded"
    row.finished_at = utcnow()
    db.commit()


def _node_runs(
    db: Session, workflow_run: WorkflowRun
) -> Dict[str, WorkflowNodeRun]:
    rows = db.scalars(
        select(WorkflowNodeRun).where(
            WorkflowNodeRun.workflow_run_id == workflow_run.id,
            WorkflowNodeRun.workspace_id == workflow_run.workspace_id,
        )
    )
    return {row.node_key: row for row in rows}


def _completed_outputs(db: Session, workflow_run: WorkflowRun) -> Dict[str, Any]:
    return {
        key: _stored_output(row)
        for key, row in _node_runs(db, workflow_run).items()
        if row.status == "succeeded"
    }


def _stored_output(row: WorkflowNodeRun) -> Any:
    if not row.output_json:
        return ""
    try:
        return json.loads(row.output_json)
    except (ValueError, TypeError):
        return row.output_json


def _succeed(db: Session, workflow_run: WorkflowRun, state: _State) -> None:
    workflow_run.status = "succeeded"
    workflow_run.error = ""
    workflow_run.finished_at = utcnow()
    _close_backing_run(db, workflow_run, "completed")
    append_event(
        db,
        workspace_id=workflow_run.workspace_id,
        run_id=state.run.id,
        event_type="run.completed",
        payload={"status": "completed", "workflow_run_id": workflow_run.id},
    )
    db.commit()


def _terminate(db: Session, workflow_run: WorkflowRun, halt: _Halt) -> None:
    """Write the terminal state and make the unrun nodes say so.

    Rolls back first: the halt may have come from a session left dirty by a
    failed flush, and a run record written on top of that is the corruption this
    function exists to prevent.
    """
    db.rollback()
    current = db.get(WorkflowRun, workflow_run.id)
    if current is None or current.status in TERMINAL:
        return
    current.status = halt.status
    current.error = f"{halt.code}: {halt.message}"[:2000]
    current.finished_at = utcnow()
    _mark_skipped(db, current, halt)
    _close_backing_run(db, current, "cancelled" if halt.status == "cancelled" else "failed")
    record_audit(
        db,
        workspace_id=current.workspace_id,
        actor_id=current.created_by,
        action="workflow_run." + halt.status,
        resource_type="workflow_run",
        resource_id=current.id,
        detail={"code": halt.code, "node": halt.node},
    )
    db.commit()


def _mark_skipped(db: Session, workflow_run: WorkflowRun, halt: _Halt) -> None:
    """Every declared node that never ran becomes a `skipped` row.

    A run detail that lists three nodes and shows two is ambiguous about the
    third; one that shows `skipped` is not. Best effort — a graph too broken to
    parse has no node list, and the terminal status is the thing that matters.
    """
    graph, _ = parse_graph(_json_object(workflow_run.graph_json))
    if graph is None:
        return
    existing = _node_runs(db, workflow_run)
    for node in graph.nodes:
        row = existing.get(node.id)
        if row is None:
            db.add(
                WorkflowNodeRun(
                    workspace_id=workflow_run.workspace_id,
                    workflow_run_id=workflow_run.id,
                    node_key=node.id,
                    kind=node.kind,
                    tool_name=node.tool,
                    status="skipped",
                    error="the run ended before this node was reached",
                )
            )
        elif row.status in {"pending", "running", "waiting_for_approval"}:
            # `running` is `failed` even when the halt names no node — a run
            # abandoned by the recovery sweep has one, and "skipped" would claim
            # the run ended before reaching a node that had already started.
            reached = row.node_key == halt.node or row.status == "running"
            row.status = "failed" if reached else "skipped"
            row.error = row.error or halt.message[:1000]
            row.finished_at = utcnow()


# --------------------------------------------------------------------------
# The backing chat run
# --------------------------------------------------------------------------


def _ensure_backing_run(
    db: Session, workflow_run: WorkflowRun, graph: WorkflowGraph
) -> Run:
    """The `Run` a workflow's tool calls, events and approvals hang off.

    One per workflow run, not one per node: `WorkflowRun.run_id` then names a
    stable row, which is what `policy_scope_for_run` and the resume dispatch both
    look up. `Run.prompt` tracks the current agent node because that is what the
    model is being asked; every node's own resolved prompt is kept on its
    `workflow_node_runs` row, so nothing is lost to the overwrite.
    """
    if workflow_run.run_id:
        run = db.get(Run, workflow_run.run_id)
        if run is not None:
            return run

    agent = db.scalar(
        select(Agent)
        .where(
            Agent.workspace_id == workflow_run.workspace_id,
            Agent.enabled.is_(True),
        )
        .order_by(Agent.created_at, Agent.id)
    )
    if agent is None:
        raise _Halt(
            "failed",
            "no_agent",
            "this workspace has no enabled agent to execute the workflow with",
        )
    conversation = Conversation(
        workspace_id=workflow_run.workspace_id,
        created_by=workflow_run.created_by,
        title=f"Workflow: {graph.name}"[:200],
    )
    db.add(conversation)
    db.flush()
    run = Run(
        workspace_id=workflow_run.workspace_id,
        conversation_id=conversation.id,
        agent_id=agent.id,
        created_by=workflow_run.created_by,
        status="queued",
        prompt=graph.description or graph.name,
    )
    db.add(run)
    db.flush()
    workflow_run.run_id = run.id
    db.commit()
    return run


def _close_backing_run(db: Session, workflow_run: WorkflowRun, status: str) -> None:
    if not workflow_run.run_id:
        return
    run = db.get(Run, workflow_run.run_id)
    if run is None or run.status in {"completed", "failed", "cancelled"}:
        return
    run.status = status
    run.lease_expires_at = None
    run.agent_state_json = None
    if status != "completed":
        run.error = workflow_run.error[:1000]


def _json_object(raw: str) -> Dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}
