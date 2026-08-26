"""Space templates and workflow templates: saved starting points.

Both halves follow the `DashboardTemplate` precedent (api/dashboards.py): a
template is a workspace-scoped snapshot, saving one is a creating POST with an
idempotency key, and instantiating one writes a brand-new row through the same
validation the hand-built path uses — `services/spaces.create_space` for a
space, `parse_graph` for a workflow — so a template can never store something
the ordinary path would have refused.

One rule is load-bearing enough to state at the top: **instantiating a
workflow template never schedules anything.** The copy lands as a draft with
`schedule_cron = ""` and `last_dispatched_at = None`, whatever the graph's
trigger says, because a template is reviewed by whoever *saved* it, not by
whoever clicks it — and "click a button, gain a cron" is exactly the unattended
surprise the workflow tier exists to prevent.

Error mapping follows the house convention: resolve the workspace-scoped
resource first so a foreign id is uniformly a 404, 409 for a taken name (the
same answer `POST /api/dashboards` gives), and 422 only for "that content is
not allowed".
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..database import get_db
from ..models import Agent, Space, SpaceTemplate, Workflow, WorkflowTemplate
from ..schemas import ApiModel, SpaceOut
from ..services import spaces
from ..services.audit import record_audit
from ..services.workflows.validate import CompileReport, parse_graph
from .dependencies import idempotency_key
from .idempotency import find_replay, record_key, replayed_resource_gone

# Underscore-shared with api/workflows.py on purpose: the instantiated copy
# must serialize exactly like every other workflow the client holds, and
# `requires_approval` must be answered against the live registry the same way,
# or the same automation would read differently on two pages.
from .workflows import WorkflowOut, _requires_approval, _workflow_out

router = APIRouter(prefix="/api", tags=["templates"])


class SpaceTemplateOut(ApiModel):
    id: str
    name: str
    description: str
    instructions: str
    #: Plain historical agent ids; a deleted agent stays listed, harmlessly.
    agent_ids: List[str]
    created_at: datetime
    updated_at: datetime


class WorkflowTemplateOut(ApiModel):
    id: str
    name: str
    description: str
    source_prompt: str
    graph: Dict[str, Any]
    created_at: datetime


class SpaceTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=500)
    #: Used when `from_space_id` is empty; ignored (the snapshot wins) otherwise.
    instructions: str = ""
    agent_ids: List[str] = Field(default_factory=list, max_length=50)
    #: "" = author from scratch; an id = snapshot that space's instructions.
    from_space_id: str = ""


class SpaceTemplateInstantiate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class WorkflowTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=500)
    #: Always a snapshot — there is no from-scratch path, because a workflow
    #: definition worth templating has already been compiled and reviewed.
    from_workflow_id: str = Field(min_length=1, max_length=64)


class WorkflowTemplateInstantiate(BaseModel):
    #: "" = keep the template's name. Workflows have no unique-name claim, so
    #: a collision is a cosmetic fact, not a conflict.
    name: str = Field(default="", max_length=160)


def _space_template_out(template: SpaceTemplate) -> SpaceTemplateOut:
    try:
        agent_ids = json.loads(template.agent_ids_json or "[]")
    except (ValueError, TypeError):
        agent_ids = []
    return SpaceTemplateOut(
        id=template.id,
        name=template.name,
        description=template.description,
        instructions=template.instructions,
        agent_ids=[item for item in agent_ids if isinstance(item, str)],
        created_at=template.created_at,
        updated_at=template.updated_at,
    )


def _workflow_template_out(template: WorkflowTemplate) -> WorkflowTemplateOut:
    try:
        graph = json.loads(template.graph_json or "{}")
    except (ValueError, TypeError):
        graph = {}
    return WorkflowTemplateOut(
        id=template.id,
        name=template.name,
        description=template.description,
        source_prompt=template.source_prompt,
        graph=graph if isinstance(graph, dict) else {},
        created_at=template.created_at,
    )


def _space_template(db: Session, actor: Actor, template_id: str) -> SpaceTemplate:
    template = db.scalar(
        select(SpaceTemplate).where(
            SpaceTemplate.id == template_id,
            SpaceTemplate.workspace_id == actor.workspace_id,
        )
    )
    if template is None:
        raise HTTPException(status_code=404, detail="Space template not found")
    return template


def _workflow_template(
    db: Session, actor: Actor, template_id: str
) -> WorkflowTemplate:
    template = db.scalar(
        select(WorkflowTemplate).where(
            WorkflowTemplate.id == template_id,
            WorkflowTemplate.workspace_id == actor.workspace_id,
        )
    )
    if template is None:
        raise HTTPException(status_code=404, detail="Workflow template not found")
    return template


def _refuse_taken_name(db: Session, model: Any, *, workspace_id: str, name: str) -> None:
    """409 on a name this workspace already holds — the `POST /api/dashboards`
    answer, and the UNIQUE(workspace_id, name) constraint said out loud."""
    existing = db.scalar(
        select(model).where(
            model.workspace_id == workspace_id, model.name == name
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="Template name already exists")


def _space_out(space: Space, db: Session, workspace_id: str) -> SpaceOut:
    threads, sources = spaces.counts_for(db, workspace_id=workspace_id).get(
        space.id, (0, 0)
    )
    return SpaceOut(
        id=space.id,
        name=space.name,
        instructions=space.instructions,
        thread_count=threads,
        source_count=sources,
        created_at=space.created_at,
        updated_at=space.updated_at,
    )


# --------------------------------------------------------------------------
# Space templates


@router.get("/space-templates", response_model=List[SpaceTemplateOut])
def list_space_templates(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[SpaceTemplateOut]:
    rows = db.scalars(
        select(SpaceTemplate)
        .where(SpaceTemplate.workspace_id == actor.workspace_id)
        .order_by(SpaceTemplate.updated_at.desc())
    )
    return [_space_template_out(row) for row in rows]


@router.post("/space-templates", response_model=SpaceTemplateOut, status_code=201)
def create_space_template(
    payload: SpaceTemplateCreate,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> SpaceTemplateOut:
    replay = find_replay(
        db,
        workspace_id=actor.workspace_id,
        operation="space_template.create",
        key=key,
    )
    if replay:
        template = db.scalar(
            select(SpaceTemplate).where(
                SpaceTemplate.id == replay.resource_id,
                SpaceTemplate.workspace_id == actor.workspace_id,
            )
        )
        if template is None:
            raise replayed_resource_gone()
        return _space_template_out(template)

    # Resolve every named resource before any other verdict, so a foreign id —
    # in the body or not — is a uniform 404 rather than a different refusal
    # that would confirm the id exists.
    instructions = payload.instructions
    if payload.from_space_id:
        source_space = db.scalar(
            select(Space).where(
                Space.id == payload.from_space_id,
                Space.workspace_id == actor.workspace_id,
            )
        )
        if source_space is None:
            raise HTTPException(status_code=404, detail="Space not found")
        instructions = source_space.instructions
    agent_ids: List[str] = []
    for agent_id in dict.fromkeys(payload.agent_ids):
        agent = db.scalar(
            select(Agent.id).where(
                Agent.id == agent_id, Agent.workspace_id == actor.workspace_id
            )
        )
        if agent is None:
            raise HTTPException(status_code=404, detail="Agent not found")
        agent_ids.append(agent_id)

    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="A template needs a name")
    _refuse_taken_name(
        db, SpaceTemplate, workspace_id=actor.workspace_id, name=name
    )
    template = SpaceTemplate(
        workspace_id=actor.workspace_id,
        created_by=actor.user_id,
        name=name,
        description=payload.description.strip(),
        instructions=instructions.strip(),
        agent_ids_json=json.dumps(agent_ids),
    )
    db.add(template)
    db.flush()
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="space_template.create",
        key=key,
        resource_id=template.id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="space_template.created",
        resource_type="space_template",
        resource_id=template.id,
        detail={"name": template.name, "from_space_id": payload.from_space_id},
    )
    db.commit()
    db.refresh(template)
    return _space_template_out(template)


@router.delete("/space-templates/{template_id}", status_code=204)
def delete_space_template(
    template_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> None:
    template = _space_template(db, actor, template_id)
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="space_template.deleted",
        resource_type="space_template",
        resource_id=template.id,
        detail={"name": template.name},
    )
    # Spaces already made from it are finished things; nothing links back.
    db.delete(template)
    db.commit()


@router.post(
    "/space-templates/{template_id}/instantiate",
    response_model=SpaceOut,
    status_code=201,
)
def instantiate_space_template(
    template_id: str,
    payload: SpaceTemplateInstantiate,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> SpaceOut:
    template = _space_template(db, actor, template_id)
    replay = find_replay(
        db,
        workspace_id=actor.workspace_id,
        operation="space_template.instantiate",
        key=key,
    )
    if replay:
        space = db.scalar(
            select(Space).where(
                Space.id == replay.resource_id,
                Space.workspace_id == actor.workspace_id,
            )
        )
        if space is None:
            raise replayed_resource_gone()
        return _space_out(space, db, actor.workspace_id)
    try:
        # The same path `POST /api/spaces` uses, so a template cannot mint a
        # space the ordinary form would refuse (dup name, over the cap...).
        space = spaces.create_space(
            db,
            workspace_id=actor.workspace_id,
            name=payload.name,
            instructions=template.instructions,
            created_by=actor.user_id,
        )
    except spaces.SpaceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="space_template.instantiate",
        key=key,
        resource_id=space.id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="space.created",
        resource_type="space",
        resource_id=space.id,
        detail={"name": space.name, "template_id": template.id},
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="space_template.instantiated",
        resource_type="space_template",
        resource_id=template.id,
        detail={"space_id": space.id, "name": space.name},
    )
    db.commit()
    db.refresh(space)
    return _space_out(space, db, actor.workspace_id)


# --------------------------------------------------------------------------
# Workflow templates


@router.get("/workflow-templates", response_model=List[WorkflowTemplateOut])
def list_workflow_templates(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[WorkflowTemplateOut]:
    rows = db.scalars(
        select(WorkflowTemplate)
        .where(WorkflowTemplate.workspace_id == actor.workspace_id)
        .order_by(WorkflowTemplate.created_at.desc())
    )
    return [_workflow_template_out(row) for row in rows]


@router.post(
    "/workflow-templates", response_model=WorkflowTemplateOut, status_code=201
)
def create_workflow_template(
    payload: WorkflowTemplateCreate,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> WorkflowTemplateOut:
    replay = find_replay(
        db,
        workspace_id=actor.workspace_id,
        operation="workflow_template.create",
        key=key,
    )
    if replay:
        template = db.scalar(
            select(WorkflowTemplate).where(
                WorkflowTemplate.id == replay.resource_id,
                WorkflowTemplate.workspace_id == actor.workspace_id,
            )
        )
        if template is None:
            raise replayed_resource_gone()
        return _workflow_template_out(template)

    workflow = db.scalar(
        select(Workflow).where(
            Workflow.id == payload.from_workflow_id,
            Workflow.workspace_id == actor.workspace_id,
        )
    )
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="A template needs a name")
    _refuse_taken_name(
        db, WorkflowTemplate, workspace_id=actor.workspace_id, name=name
    )
    template = WorkflowTemplate(
        workspace_id=actor.workspace_id,
        created_by=actor.user_id,
        name=name,
        description=payload.description.strip(),
        # The whole definition, verbatim: the graph says what will run, the
        # prompt says what someone asked for, and drift between an instantiated
        # copy and the ask is only visible while both survive.
        graph_json=workflow.graph_json,
        source_prompt=workflow.source_prompt,
    )
    db.add(template)
    db.flush()
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="workflow_template.create",
        key=key,
        resource_id=template.id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="workflow_template.created",
        resource_type="workflow_template",
        resource_id=template.id,
        detail={"name": template.name, "from_workflow_id": workflow.id},
    )
    db.commit()
    db.refresh(template)
    return _workflow_template_out(template)


@router.delete("/workflow-templates/{template_id}", status_code=204)
def delete_workflow_template(
    template_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> None:
    template = _workflow_template(db, actor, template_id)
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="workflow_template.deleted",
        resource_type="workflow_template",
        resource_id=template.id,
        detail={"name": template.name},
    )
    db.delete(template)
    db.commit()


@router.post(
    "/workflow-templates/{template_id}/instantiate",
    response_model=WorkflowOut,
    status_code=201,
)
def instantiate_workflow_template(
    template_id: str,
    payload: WorkflowTemplateInstantiate,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> WorkflowOut:
    template = _workflow_template(db, actor, template_id)
    replay = find_replay(
        db,
        workspace_id=actor.workspace_id,
        operation="workflow_template.instantiate",
        key=key,
    )
    if replay:
        workflow = db.scalar(
            select(Workflow).where(
                Workflow.id == replay.resource_id,
                Workflow.workspace_id == actor.workspace_id,
            )
        )
        if workflow is None:
            raise replayed_resource_gone()
        return _workflow_out(
            workflow, requires_approval=_requires_approval(workflow, db, actor)
        )

    try:
        document = json.loads(template.graph_json or "{}")
    except (ValueError, TypeError):
        document = None
    # Re-validated at the moment of copying, not trusted from the snapshot: a
    # template saved under an older graph schema must fail here, legibly, and
    # not at 3am inside the executor.
    graph, errors = parse_graph(document)
    if graph is None:
        raise HTTPException(
            status_code=422, detail=CompileReport(errors=errors).as_dict()
        )

    workflow = Workflow(
        workspace_id=actor.workspace_id,
        created_by=actor.user_id,
        name=(payload.name.strip() or template.name)[:160],
        description=graph.description,
        source_prompt=template.source_prompt,
        graph_json=template.graph_json,
        version=1,
        # Draft, manual, no cron, never dispatched: whatever the graph's
        # trigger declares, the *row* is what the ticker reads, and this row
        # can never fire until a person reviews and activates the copy.
        status="draft",
        trigger_kind="manual",
        schedule_cron="",
        schedule_timezone="UTC",
        last_dispatched_at=None,
    )
    db.add(workflow)
    db.flush()
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="workflow_template.instantiate",
        key=key,
        resource_id=workflow.id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="workflow.created",
        resource_type="workflow",
        resource_id=workflow.id,
        detail={"name": workflow.name, "template_id": template.id},
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="workflow_template.instantiated",
        resource_type="workflow_template",
        resource_id=template.id,
        detail={"workflow_id": workflow.id, "name": workflow.name},
    )
    db.commit()
    return _workflow_out(
        workflow, requires_approval=_requires_approval(workflow, db, actor)
    )
