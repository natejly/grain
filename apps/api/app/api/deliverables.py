"""REST surface over deliverable runs and their manifests.

One start route and two reads is the whole surface. The run itself is an
ordinary `WorkflowRun` and is watched on the workflow pages that already exist
— a second run-watching surface would be a second thing to keep correct.

The manifest's value is the join between a file and the evidence behind it, so
the detail response embeds the coverage ledger rather than making a reader
fetch it separately (which is why there is no
`GET /api/deliverables/{id}/coverage`).
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..database import get_db
from ..models import CoverageLedger, DeliverableManifest, ManifestFile, Space, WorkflowRun
from ..schemas import ApiModel
from ..services import coverage as coverage_service
from ..services import deliverables as service
from ..services.audit import record_audit
from ..services.workflows import executor
from .coverage import CoverageLedgerOut, ledger_out
from .dependencies import idempotency_key
from .idempotency import find_replay, record_key, replayed_resource_gone

router = APIRouter(prefix="/api/deliverables", tags=["deliverables"])


class ManifestFileOut(ApiModel):
    ordinal: int
    #: The `Source` `sandbox_download` created. Its status is "stored", not
    #: "ready": the file is downloadable and is NOT retrievable or quotable.
    source_id: str
    filename: str
    byte_size: int
    sandbox_session_id: str
    #: The `search_sources` queries this run asked.
    queries: List[str] = []
    #: The passages this run cited. Cited, not merely returned — what a search
    #: handed the model is not persisted per call, and a weaker join stated
    #: honestly beats a stronger one invented.
    chunk_ids: List[str] = []


class ManifestOut(ApiModel):
    id: str
    workflow_run_id: str
    #: '' is the workspace library, never NULL.
    space_id: str
    title: str
    #: "complete" | "partial". Partial is a real outcome, not an error: the run
    #: halted — budget exhausted, cancelled or failed — and shipped the files
    #: that did land. WHICH of those it was is read off the numbers below
    #: rather than asserted here: a client says "budget exhausted" only when
    #: the spend actually reached the budget.
    status: str
    #: What the run was given. 0 means no limit, explicitly — the start request
    #: allows it, and the executor treats it as unlimited rather than as an
    #: instant halt.
    budget_seconds: int
    budget_tool_calls: int
    #: What it spent. `spent_tool_calls` counts EXECUTED calls only: a call the
    #: approval gate parked and a human denied cost the run nothing.
    spent_seconds: int
    spent_tool_calls: int
    created_by: str
    created_at: datetime


class ManifestDetailOut(ApiModel):
    manifest: ManifestOut
    files: List[ManifestFileOut] = []
    #: None means no coverage was recorded for this run — common, and not an
    #: error. A client must render it as "no coverage recorded", never as a
    #: failure.
    coverage: Optional[CoverageLedgerOut] = None


class DeliverableStartRequest(BaseModel):
    """What to research, where, and how much of it to pay for.

    Both budgets are ENFORCED, between nodes, by the executor
    (`workflows.executor._enforce_budget`), and both are recorded on the
    manifest beside what the run actually spent. 0 means no limit.
    """

    title: str = Field(default="", max_length=200)
    question: str = Field(min_length=1, max_length=4000)
    space_id: str = Field(default="", max_length=36)
    budget_seconds: int = Field(default=service.DEFAULT_BUDGET_SECONDS, ge=0, le=86400)
    budget_tool_calls: int = Field(
        default=service.DEFAULT_BUDGET_TOOL_CALLS, ge=0, le=1000
    )


class DeliverableStartedOut(ApiModel):
    workflow_id: str
    workflow_run_id: str


def _ids(raw: str) -> List[str]:
    try:
        parsed = json.loads(raw or "[]")
    except ValueError:
        return []
    return [item for item in parsed if isinstance(item, str)]


def _out(manifest: DeliverableManifest) -> ManifestOut:
    return ManifestOut(
        id=manifest.id,
        workflow_run_id=manifest.workflow_run_id,
        space_id=manifest.space_id,
        title=manifest.title,
        status=manifest.status,
        budget_seconds=manifest.budget_seconds,
        budget_tool_calls=manifest.budget_tool_calls,
        spent_seconds=manifest.spent_seconds,
        spent_tool_calls=manifest.spent_tool_calls,
        created_by=manifest.created_by,
        created_at=manifest.created_at,
    )


def _detail(db: Session, manifest: DeliverableManifest) -> ManifestDetailOut:
    files = list(
        db.scalars(
            select(ManifestFile)
            .where(
                ManifestFile.manifest_id == manifest.id,
                ManifestFile.workspace_id == manifest.workspace_id,
            )
            .order_by(ManifestFile.ordinal)
        )
    )
    coverage: Optional[CoverageLedgerOut] = None
    if manifest.ledger_id:
        ledger = db.scalar(
            select(CoverageLedger).where(
                CoverageLedger.id == manifest.ledger_id,
                CoverageLedger.workspace_id == manifest.workspace_id,
            )
        )
        if ledger is not None:
            coverage = ledger_out(
                db, ledger, coverage_service.entries_for(db, ledger=ledger)
            )
    return ManifestDetailOut(
        manifest=_out(manifest),
        files=[
            ManifestFileOut(
                ordinal=row.ordinal,
                source_id=row.source_id,
                filename=row.filename,
                byte_size=row.byte_size,
                sandbox_session_id=row.sandbox_session_id,
                queries=_ids(row.queries_json),
                chunk_ids=_ids(row.chunk_ids_json),
            )
            for row in files
        ],
        coverage=coverage,
    )


@router.post("", response_model=DeliverableStartedOut, status_code=202)
def start_deliverable(
    payload: DeliverableStartRequest,
    background_tasks: BackgroundTasks,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> DeliverableStartedOut:
    replay = find_replay(
        db, workspace_id=actor.workspace_id, operation="deliverable.start", key=key
    )
    if replay:
        run = db.scalar(
            select(WorkflowRun).where(
                WorkflowRun.id == replay.resource_id,
                WorkflowRun.workspace_id == actor.workspace_id,
            )
        )
        if run is None:
            raise replayed_resource_gone()
        return DeliverableStartedOut(
            workflow_id=run.workflow_id, workflow_run_id=run.id
        )
    if payload.space_id:
        # Resolved under the caller's workspace FIRST: a foreign space 404s
        # before a workflow is written, so a run can never be attached to a
        # space its starter cannot see.
        found = db.scalar(
            select(Space.id).where(
                Space.id == payload.space_id,
                Space.workspace_id == actor.workspace_id,
            )
        )
        if found is None:
            raise HTTPException(status_code=404, detail="Space not found")
    workflow = service.instantiate(
        db,
        workspace_id=actor.workspace_id,
        user_id=actor.user_id,
        title=payload.title or payload.question,
    )
    workflow_run = executor.start_run(
        db,
        workflow,
        user_id=actor.user_id,
        trigger="manual",
        payload={
            "question": payload.question,
            "space_id": payload.space_id,
            "budget_seconds": payload.budget_seconds,
            "budget_tool_calls": payload.budget_tool_calls,
        },
    )
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="deliverable.start",
        key=key,
        resource_id=workflow_run.id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="deliverable.started",
        resource_type="workflow_run",
        resource_id=workflow_run.id,
        detail={
            "space_id": payload.space_id,
            "budget_seconds": payload.budget_seconds,
            "budget_tool_calls": payload.budget_tool_calls,
        },
    )
    db.commit()
    background_tasks.add_task(executor.process_workflow_run, workflow_run.id)
    return DeliverableStartedOut(
        workflow_id=workflow.id, workflow_run_id=workflow_run.id
    )


@router.get("", response_model=List[ManifestOut])
def list_deliverables(
    space_id: Optional[str] = None,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[ManifestOut]:
    query = select(DeliverableManifest).where(
        DeliverableManifest.workspace_id == actor.workspace_id
    )
    if space_id is not None:
        query = query.where(DeliverableManifest.space_id == space_id)
    rows = list(
        db.scalars(
            query.order_by(
                DeliverableManifest.created_at.desc(), DeliverableManifest.id
            )
        )
    )
    return [_out(manifest) for manifest in rows]


@router.get("/{manifest_id}", response_model=ManifestDetailOut)
def get_deliverable(
    manifest_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> ManifestDetailOut:
    manifest = db.scalar(
        select(DeliverableManifest).where(
            DeliverableManifest.id == manifest_id,
            DeliverableManifest.workspace_id == actor.workspace_id,
        )
    )
    if manifest is None:
        raise HTTPException(status_code=404, detail="Manifest not found")
    return _detail(db, manifest)
