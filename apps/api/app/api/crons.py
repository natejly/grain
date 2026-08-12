"""REST surface over personal crons (Automations).

Six workspace-scoped routes. They own no lookup logic: `_load` resolves an id
inside the caller's workspace or 404s, and every query downstream filters on
`workspace_id` again, so a foreign id is indistinguishable from a missing one —
the same posture as `workflows.py`, and for the same reason.

A cron carries no authority of its own. Creating one writes no `tool_policies`
row and grants nothing; a `kind="task"` fire runs at `workflow` policy scope
(via `Run.cron_id`, read by `policy_scope_for_run`), so the first write-capable
tool parks for a human exactly as a scheduled workflow does. `run-now` is the
one route that acts immediately, and it is claim-free by design — a person is
asking for it now — but it still routes through the same unattended gate.

Dispatch is not here: the shared `POST /api/workflows/tick` claims and enqueues
due crons through `services/crons.py`. One external cron, one secret, one URL.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..database import get_db
from ..models import Cron
from ..schemas import ApiModel
from ..services import crons as cron_service
from ..services.audit import record_audit
from ..services.runs import process_run
from ..services.workflows.validate import cron_error

router = APIRouter(prefix="/api/crons", tags=["crons"])


class CronOut(ApiModel):
    id: str
    name: str
    kind: str
    schedule_cron: str
    schedule_timezone: str
    enabled: bool
    prompt: str
    body: str
    target_conversation_id: str
    last_dispatched_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


class CronCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    kind: Literal["task", "message"] = "task"
    schedule_cron: str = Field(min_length=1, max_length=120)
    schedule_timezone: str = Field(default="UTC", max_length=64)
    prompt: str = Field(default="", max_length=8000)
    body: str = Field(default="", max_length=8000)


class CronUpdateRequest(BaseModel):
    """Every field optional — a PATCH sets only what it names.

    `enabled` lives here too: an enable/disable is a one-field PATCH, so there is
    no separate route to keep in sync with it.
    """

    name: Optional[str] = Field(default=None, min_length=1, max_length=160)
    kind: Optional[Literal["task", "message"]] = None
    schedule_cron: Optional[str] = Field(default=None, min_length=1, max_length=120)
    schedule_timezone: Optional[str] = Field(default=None, max_length=64)
    prompt: Optional[str] = Field(default=None, max_length=8000)
    body: Optional[str] = Field(default=None, max_length=8000)
    enabled: Optional[bool] = None


class CronRunNowOut(ApiModel):
    #: The task run this fire started, or "" for a message cron (which posts
    #: synchronously and has no run to watch).
    run_id: str


def _out(cron: Cron) -> CronOut:
    return CronOut(
        id=cron.id,
        name=cron.name,
        kind=cron.kind,
        schedule_cron=cron.schedule_cron,
        schedule_timezone=cron.schedule_timezone,
        enabled=cron.enabled,
        prompt=cron.prompt,
        body=cron.body,
        target_conversation_id=cron.target_conversation_id,
        last_dispatched_at=cron.last_dispatched_at,
        created_at=cron.created_at,
        updated_at=cron.updated_at,
    )


def _load(db: Session, actor: Actor, cron_id: str) -> Cron:
    """Resolve a cron id inside the caller's workspace, or 404.

    404 and not 403: a 403 would confirm the id names a real automation in
    somebody else's workspace, which is the fact worth withholding.
    """
    cron = db.scalar(
        select(Cron).where(
            Cron.id == cron_id,
            Cron.workspace_id == actor.workspace_id,
        )
    )
    if cron is None:
        raise HTTPException(status_code=404, detail="Cron not found")
    return cron


def _validate_schedule(schedule_cron: str, timezone: str) -> None:
    """422 a bad cron or IANA zone at the boundary, exactly as a workflow trigger
    is validated — so the refusal lands where a person is still holding the form,
    not silently inside a tick that never fires."""
    error = cron_error(schedule_cron)
    if error is not None:
        raise HTTPException(status_code=422, detail=error)
    try:
        ZoneInfo(timezone or "UTC")
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise HTTPException(
            status_code=422, detail=f"unknown timezone “{timezone}”"
        ) from exc


@router.get("", response_model=List[CronOut])
def list_crons(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[CronOut]:
    rows = list(
        db.scalars(
            select(Cron)
            .where(Cron.workspace_id == actor.workspace_id)
            .order_by(Cron.created_at.desc())
        )
    )
    return [_out(row) for row in rows]


@router.post("", response_model=CronOut, status_code=201)
def create_cron(
    payload: CronCreateRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> CronOut:
    _validate_schedule(payload.schedule_cron, payload.schedule_timezone)
    cron = Cron(
        workspace_id=actor.workspace_id,
        created_by=actor.user_id,
        name=payload.name,
        kind=payload.kind,
        schedule_cron=payload.schedule_cron,
        schedule_timezone=payload.schedule_timezone or "UTC",
        prompt=payload.prompt,
        body=payload.body,
    )
    db.add(cron)
    db.flush()
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="cron.created",
        resource_type="cron",
        resource_id=cron.id,
        detail={"kind": cron.kind, "schedule_cron": cron.schedule_cron},
    )
    db.commit()
    return _out(cron)


@router.get("/{cron_id}", response_model=CronOut)
def get_cron(
    cron_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> CronOut:
    return _out(_load(db, actor, cron_id))


@router.patch("/{cron_id}", response_model=CronOut)
def update_cron(
    cron_id: str,
    payload: CronUpdateRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> CronOut:
    cron = _load(db, actor, cron_id)
    fields = payload.model_dump(exclude_unset=True)
    # Validate the schedule against the *resulting* pair, so a PATCH that changes
    # only the zone is still checked against the stored cron and vice versa.
    if "schedule_cron" in fields or "schedule_timezone" in fields:
        _validate_schedule(
            fields.get("schedule_cron", cron.schedule_cron),
            fields.get("schedule_timezone", cron.schedule_timezone),
        )
    for key, value in fields.items():
        setattr(cron, key, value)
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="cron.updated",
        resource_type="cron",
        resource_id=cron.id,
        detail={"fields": sorted(fields.keys())},
    )
    db.commit()
    return _out(cron)


@router.delete("/{cron_id}", status_code=204)
def delete_cron(
    cron_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> None:
    cron = _load(db, actor, cron_id)
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="cron.deleted",
        resource_type="cron",
        resource_id=cron.id,
        detail={"kind": cron.kind},
    )
    db.delete(cron)
    db.commit()


@router.post("/{cron_id}/run-now", response_model=CronRunNowOut, status_code=202)
def run_cron_now(
    cron_id: str,
    background_tasks: BackgroundTasks,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> CronRunNowOut:
    """Fire the cron immediately, without touching the atomic claim.

    Claim-free on purpose: the claim exists to make the unattended ticker fire
    once per minute, and a person pressing "run now" is asking for exactly one
    more fire regardless of when the last tick was. A `kind="task"` still runs at
    `workflow` policy scope and parks on a write — pressing the button is not
    standing next to the write.
    """
    cron = _load(db, actor, cron_id)
    run_id = cron_service.fire(db, cron)
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="cron.run_now",
        resource_type="cron",
        resource_id=cron.id,
        detail={"kind": cron.kind, "run_id": run_id or ""},
    )
    db.commit()
    if run_id:
        background_tasks.add_task(process_run, run_id)
    return CronRunNowOut(run_id=run_id or "")
