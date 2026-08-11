"""REST surface over workflow automations (ADR 0007).

Six workspace-scoped routes and one that is not. The six own no lookup logic:
`_load` resolves an id inside the caller's workspace or 404s, and every query
downstream filters on `workspace_id` again, so a foreign id is indistinguishable
from a missing one — the same posture as `sandbox.py`, and for the same reason.

Two things here are worth reading before changing anything.

**Compiling is not authorising.** `POST /compile` runs the model and the
validator and returns a graph. It writes no `tool_policies` row, creates no run
and, on its own, saves nothing. Every route that eventually executes something
does so through `services/workflows/executor`, which resolves every tool call at
`workflow` policy scope — so a workflow cannot become a way of doing something
the workspace has not separately agreed a workflow may do.

**`POST /tick` is the one unauthenticated route, and it is a claim.** It carries
a shared secret, it takes no arguments, it cannot name a workflow or a time, and
it fires nothing that the workspace's own cron expression did not already say to
fire. With `WORKFLOW_CRON_SECRET` unset it refuses outright, which keeps ADR
0007's warning accurate for any deployment that has not deliberately turned
scheduling on.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..clock import utcnow
from ..config import Settings, get_settings
from ..database import get_db
from ..models import Workflow, WorkflowNodeRun, WorkflowRun
from ..schemas import ApiModel
from ..services.audit import record_audit
from ..services.llm_tools import ToolContext, build_registry
from ..services.workflows import (
    CompiledWorkflow,
    WorkflowCompileError,
    compile_document,
    compile_workflow,
    executor,
    schedule,
    summarize,
)

router = APIRouter(prefix="/api/workflows", tags=["workflows"])

# A run list is a UI panel, not an export. A workflow ticking every five minutes
# accumulates thousands of runs and nobody scrolls past the first screen.
MAX_RUNS = 200

MAX_PROMPT_CHARS = 4000


class WorkflowOut(ApiModel):
    id: str
    name: str
    description: str
    source_prompt: str
    graph: Dict[str, Any]
    version: int
    status: str
    trigger_kind: str
    schedule_cron: str
    schedule_timezone: str
    #: True when at least one node names a write-capable tool, i.e. this
    #: workflow will park for a human rather than complete unattended.
    requires_approval: bool
    last_dispatched_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


class WorkflowNodeRunOut(ApiModel):
    node_key: str
    kind: str
    tool_name: str
    status: str
    attempt: int
    arguments: Dict[str, Any]
    output: str
    #: What authorised this node — `allow` for a standing grant or a read-only
    #: tool, `ask` for a human decision on this specific call. ADR 0007: without
    #: it the trail cannot tell an unattended 3am write from an approved one.
    policy: str
    agent_tool_call_id: Optional[str]
    error: str
    latency_ms: int
    started_at: Optional[datetime]
    finished_at: Optional[datetime]


class WorkflowRunOut(ApiModel):
    id: str
    workflow_id: str
    workflow_version: int
    trigger: str
    status: str
    run_id: Optional[str]
    error: str
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    created_at: datetime


class WorkflowRunDetailOut(WorkflowRunOut):
    nodes: List[WorkflowNodeRunOut]


class WorkflowCompileOut(ApiModel):
    # Every model here is prefixed, because FastAPI keys its component schemas by
    # class name: a second `RunRequest` renames the sandbox's one to a
    # module-qualified string and churns every generated client that referenced it.
    graph: Dict[str, Any]
    summary: Dict[str, Any]


class WorkflowTickOut(ApiModel):
    dispatched: List[str]
    moment: datetime


class WorkflowCompileRequest(BaseModel):
    source_prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)


class WorkflowCreateRequest(BaseModel):
    """Either a sentence to compile or an already-formed graph, never neither.

    A hand-written graph goes through `compile_document`, the same function the
    model's output goes through, so a person cannot store something a model
    would have been refused for.
    """

    source_prompt: str = ""
    graph: Optional[Dict[str, Any]] = None
    status: Literal["draft", "active", "disabled"] = "draft"


class WorkflowUpdateRequest(BaseModel):
    status: Optional[Literal["draft", "active", "disabled"]] = None
    #: Recompiling bumps `version`; runs already recorded keep the graph they
    #: executed, so history does not move under a later edit.
    source_prompt: Optional[str] = Field(default=None, max_length=MAX_PROMPT_CHARS)
    graph: Optional[Dict[str, Any]] = None


class WorkflowRunRequest(BaseModel):
    #: The `{{ input.field }}` namespace for this execution.
    payload: Dict[str, Any] = Field(default_factory=dict)


def _graph_document(workflow: Workflow) -> Dict[str, Any]:
    try:
        value = json.loads(workflow.graph_json or "{}")
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _requires_approval(workflow: Workflow, db: Session, actor: Actor) -> bool:
    """Would this workflow park? Answered against the *live* registry.

    A stored flag would go stale the moment an MCP server was reconnected with a
    tool of a different disposition, and "will this run unattended" is exactly
    the question nobody should be given a stale answer to.
    """
    document = _graph_document(workflow)
    context = ToolContext(
        workspace_id=actor.workspace_id,
        user_id=actor.user_id,
        conversation_id="",
    )
    try:
        compiled = compile_document(document, build_registry(db, context))
    except WorkflowCompileError:
        # A graph that no longer validates cannot be characterised; the run
        # detail is where that gets reported, loudly, at run start.
        return True
    return compiled.requires_approval


def _workflow_out(workflow: Workflow, *, requires_approval: bool) -> WorkflowOut:
    return WorkflowOut(
        id=workflow.id,
        name=workflow.name,
        description=workflow.description,
        source_prompt=workflow.source_prompt,
        graph=_graph_document(workflow),
        version=workflow.version,
        status=workflow.status,
        trigger_kind=workflow.trigger_kind,
        schedule_cron=workflow.schedule_cron,
        schedule_timezone=workflow.schedule_timezone,
        requires_approval=requires_approval,
        last_dispatched_at=workflow.last_dispatched_at,
        created_at=workflow.created_at,
        updated_at=workflow.updated_at,
    )


def _run_out(row: WorkflowRun) -> WorkflowRunOut:
    return WorkflowRunOut(
        id=row.id,
        workflow_id=row.workflow_id,
        workflow_version=row.workflow_version,
        trigger=row.trigger,
        status=row.status,
        run_id=row.run_id,
        error=row.error,
        started_at=row.started_at,
        finished_at=row.finished_at,
        created_at=row.created_at,
    )


def _node_out(row: WorkflowNodeRun) -> WorkflowNodeRunOut:
    try:
        arguments = json.loads(row.arguments_json or "{}")
    except (ValueError, TypeError):
        arguments = {}
    try:
        output = json.loads(row.output_json) if row.output_json else ""
    except (ValueError, TypeError):
        output = row.output_json
    return WorkflowNodeRunOut(
        node_key=row.node_key,
        kind=row.kind,
        tool_name=row.tool_name,
        status=row.status,
        attempt=row.attempt,
        arguments=arguments if isinstance(arguments, dict) else {},
        output=output if isinstance(output, str) else json.dumps(output, default=str),
        policy=row.policy,
        agent_tool_call_id=row.agent_tool_call_id,
        error=row.error,
        latency_ms=row.latency_ms,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def _load(db: Session, actor: Actor, workflow_id: str) -> Workflow:
    """Resolve a workflow id inside the caller's workspace, or 404.

    404 and not 403: a 403 would confirm that the id names a real automation in
    somebody else's workspace, which is the fact worth withholding.
    """
    workflow = db.scalar(
        select(Workflow).where(
            Workflow.id == workflow_id,
            Workflow.workspace_id == actor.workspace_id,
        )
    )
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return workflow


def _compile_failure(exc: WorkflowCompileError) -> HTTPException:
    """422 with every finding, not the first.

    The caller's next move is to show a person the whole list or to hand it back
    to a model as a repair prompt, and both are worse one error at a time.
    """
    return HTTPException(status_code=422, detail=exc.report.as_dict())


def _apply(workflow: Workflow, compiled: CompiledWorkflow, *, source_prompt: str) -> None:
    graph = compiled.graph
    workflow.name = graph.name[:160] or "Untitled workflow"
    workflow.description = graph.description
    workflow.source_prompt = source_prompt
    workflow.graph_json = json.dumps(graph.to_document(), default=str)
    workflow.trigger_kind = graph.trigger.kind
    workflow.schedule_cron = graph.trigger.cron
    workflow.schedule_timezone = graph.trigger.timezone


@router.post("/compile", response_model=WorkflowCompileOut)
def compile_workflow_preview(
    payload: WorkflowCompileRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> WorkflowCompileOut:
    """Natural language to a validated DAG, without saving anything.

    The preview a person reads before deciding whether to store an automation.
    It grants nothing and creates nothing; a compiled workflow is a proposal.
    """
    context = ToolContext(
        workspace_id=actor.workspace_id,
        user_id=actor.user_id,
        conversation_id="",
    )
    try:
        compiled = compile_workflow(
            db, context, source_prompt=payload.source_prompt, settings=settings
        )
    except WorkflowCompileError as exc:
        raise _compile_failure(exc) from exc
    return WorkflowCompileOut(graph=compiled.graph.to_document(), summary=summarize(compiled))


@router.get("", response_model=List[WorkflowOut])
def list_workflows(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[WorkflowOut]:
    rows = list(
        db.scalars(
            select(Workflow)
            .where(Workflow.workspace_id == actor.workspace_id)
            .order_by(Workflow.created_at.desc())
        )
    )
    return [
        _workflow_out(row, requires_approval=_requires_approval(row, db, actor))
        for row in rows
    ]


@router.post("", response_model=WorkflowOut, status_code=201)
def create_workflow(
    payload: WorkflowCreateRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> WorkflowOut:
    """Compile and store an automation."""
    if payload.graph is None and not payload.source_prompt.strip():
        raise HTTPException(
            status_code=422, detail="Give a source_prompt to compile, or a graph."
        )
    context = ToolContext(
        workspace_id=actor.workspace_id,
        user_id=actor.user_id,
        conversation_id="",
    )
    registry = build_registry(db, context)
    try:
        compiled = (
            compile_document(payload.graph, registry)
            if payload.graph is not None
            else compile_workflow(
                db,
                context,
                source_prompt=payload.source_prompt,
                settings=settings,
                registry=registry,
            )
        )
    except WorkflowCompileError as exc:
        raise _compile_failure(exc) from exc

    workflow = Workflow(
        workspace_id=actor.workspace_id,
        created_by=actor.user_id,
        name=compiled.graph.name,
        graph_json="{}",
        version=1,
        status=payload.status,
    )
    _apply(workflow, compiled, source_prompt=payload.source_prompt)
    db.add(workflow)
    db.flush()
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="workflow.created",
        resource_type="workflow",
        resource_id=workflow.id,
        detail={
            "trigger": workflow.trigger_kind,
            "requires_approval": compiled.requires_approval,
        },
    )
    db.commit()
    return _workflow_out(workflow, requires_approval=compiled.requires_approval)


@router.get("/{workflow_id}", response_model=WorkflowOut)
def get_workflow(
    workflow_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> WorkflowOut:
    workflow = _load(db, actor, workflow_id)
    return _workflow_out(
        workflow, requires_approval=_requires_approval(workflow, db, actor)
    )


@router.patch("/{workflow_id}", response_model=WorkflowOut)
def update_workflow(
    workflow_id: str,
    payload: WorkflowUpdateRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> WorkflowOut:
    """Enable, disable, or recompile.

    A recompile bumps `version` and leaves `source_prompt` beside the graph, so
    a graph that has drifted from what somebody asked for is still visible as
    drift rather than as the new truth.
    """
    workflow = _load(db, actor, workflow_id)
    context = ToolContext(
        workspace_id=actor.workspace_id,
        user_id=actor.user_id,
        conversation_id="",
    )
    compiled: Optional[CompiledWorkflow] = None
    if payload.graph is not None or payload.source_prompt is not None:
        registry = build_registry(db, context)
        try:
            compiled = (
                compile_document(payload.graph, registry)
                if payload.graph is not None
                else compile_workflow(
                    db,
                    context,
                    source_prompt=payload.source_prompt or "",
                    settings=settings,
                    registry=registry,
                )
            )
        except WorkflowCompileError as exc:
            raise _compile_failure(exc) from exc
        _apply(
            workflow,
            compiled,
            source_prompt=payload.source_prompt or workflow.source_prompt,
        )
        workflow.version += 1
    if payload.status is not None:
        workflow.status = payload.status
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="workflow.updated",
        resource_type="workflow",
        resource_id=workflow.id,
        detail={"status": workflow.status, "version": workflow.version},
    )
    db.commit()
    return _workflow_out(
        workflow,
        requires_approval=(
            compiled.requires_approval
            if compiled is not None
            else _requires_approval(workflow, db, actor)
        ),
    )


@router.delete("/{workflow_id}", status_code=204)
def delete_workflow(
    workflow_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> None:
    """Delete the workflow and its run history.

    History goes with it deliberately. A run detail whose graph has been deleted
    can no longer say what it executed, and a half-readable audit trail invites
    more confident conclusions than it can support.
    """
    workflow = _load(db, actor, workflow_id)
    run_ids = list(
        db.scalars(
            select(WorkflowRun.id).where(
                WorkflowRun.workflow_id == workflow.id,
                WorkflowRun.workspace_id == actor.workspace_id,
            )
        )
    )
    if run_ids:
        # Children first, and the workspace filter is repeated on both: the id
        # list came from a scoped query, and the redundancy is what stops that
        # ordering from being edited away into a cross-tenant delete.
        for node_row in db.scalars(
            select(WorkflowNodeRun).where(
                WorkflowNodeRun.workflow_run_id.in_(run_ids),
                WorkflowNodeRun.workspace_id == actor.workspace_id,
            )
        ):
            db.delete(node_row)
        for run_row in db.scalars(
            select(WorkflowRun).where(
                WorkflowRun.id.in_(run_ids),
                WorkflowRun.workspace_id == actor.workspace_id,
            )
        ):
            db.delete(run_row)
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="workflow.deleted",
        resource_type="workflow",
        resource_id=workflow.id,
        detail={"runs": len(run_ids)},
    )
    db.delete(workflow)
    db.commit()


@router.post("/{workflow_id}/run", response_model=WorkflowRunOut, status_code=202)
def run_workflow(
    workflow_id: str,
    payload: WorkflowRunRequest,
    background_tasks: BackgroundTasks,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> WorkflowRunOut:
    """Start a run now. 202: the graph executes on a background task.

    A manual run is still policy-gated at `workflow` scope. Pressing the button
    is not standing next to the write — the graph may take a minute to reach it,
    and by then the person who pressed it has moved on.
    """
    workflow = _load(db, actor, workflow_id)
    if workflow.status == "disabled":
        raise HTTPException(status_code=409, detail="This workflow is disabled")
    workflow_run = executor.start_run(
        db, workflow, user_id=actor.user_id, trigger="manual", payload=payload.payload
    )
    db.commit()
    background_tasks.add_task(executor.process_workflow_run, workflow_run.id)
    return _run_out(workflow_run)


@router.get("/{workflow_id}/runs", response_model=List[WorkflowRunOut])
def list_workflow_runs(
    workflow_id: str,
    limit: int = Query(default=50, ge=1, le=MAX_RUNS),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[WorkflowRunOut]:
    workflow = _load(db, actor, workflow_id)
    rows = list(
        db.scalars(
            select(WorkflowRun)
            .where(
                WorkflowRun.workflow_id == workflow.id,
                WorkflowRun.workspace_id == actor.workspace_id,
            )
            .order_by(WorkflowRun.created_at.desc())
            .limit(limit)
        )
    )
    return [_run_out(row) for row in rows]


@router.get("/runs/{workflow_run_id}", response_model=WorkflowRunDetailOut)
def get_workflow_run(
    workflow_run_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> WorkflowRunDetailOut:
    """One run, with every node's state — including the ones that never ran."""
    row = db.scalar(
        select(WorkflowRun).where(
            WorkflowRun.id == workflow_run_id,
            WorkflowRun.workspace_id == actor.workspace_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Workflow run not found")
    nodes = list(
        db.scalars(
            select(WorkflowNodeRun)
            .where(
                WorkflowNodeRun.workflow_run_id == row.id,
                WorkflowNodeRun.workspace_id == actor.workspace_id,
            )
            .order_by(WorkflowNodeRun.created_at, WorkflowNodeRun.node_key)
        )
    )
    return WorkflowRunDetailOut(
        **_run_out(row).model_dump(), nodes=[_node_out(node) for node in nodes]
    )


@router.post("/tick", response_model=WorkflowTickOut)
def tick(
    background_tasks: BackgroundTasks,
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> WorkflowTickOut:
    """Dispatch every workflow whose cron fires now. For an external cron only.

    Unauthenticated by necessity — a cron has no session — and therefore given
    the smallest possible surface: no arguments, no workflow id, no timestamp,
    and a body that names only what it started. The secret is compared with
    `secrets.compare_digest`, and its absence is a 503 rather than an open door.
    """
    configured = settings.workflow_cron_secret
    if configured is None or not configured.get_secret_value():
        raise HTTPException(
            status_code=503, detail="Workflow scheduling is not configured"
        )
    presented = authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(presented, configured.get_secret_value()):
        raise HTTPException(status_code=401, detail="Not authorised")

    started = schedule.dispatch_due(db)
    for workflow_run in started:
        background_tasks.add_task(executor.process_workflow_run, workflow_run.id)
    return WorkflowTickOut(
        dispatched=[workflow_run.id for workflow_run in started],
        moment=schedule.floor_minute(utcnow()),
    )
