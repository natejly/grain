"""REST surface over metric monitors (Automations).

Five workspace-scoped routes on the crons router's posture: `_load` resolves an
id inside the caller's workspace or 404s, and a foreign id is indistinguishable
from a missing one. The dataset a monitor names is resolved under the caller's
workspace *first*, so building a monitor over another tenant's dataset answers
404 before any validation could say something more revealing.

A monitor carries no authority: it reads a dataset and writes a notification on
the ok→tripped edge, nothing else. `run-now` is claim-free — a person asking now
deserves an answer now — and returns the evaluation's honest outcome, including
"skipped" with its reason, rather than pretending a broken query evaluated.

Dispatch is not here: the shared `POST /api/workflows/tick` claims and evaluates
due monitors through `services/monitors.py`. One external cron, one secret.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..database import get_db
from ..models import Dataset, Monitor
from ..schemas import ApiModel, DatasetQuery
from ..services import monitors as monitor_service
from ..services.audit import record_audit
from ..services.workflows.validate import cron_error
from .dependencies import idempotency_key
from .idempotency import find_replay, record_key, replayed_resource_gone

router = APIRouter(prefix="/api/monitors", tags=["monitors"])


class MonitorOut(ApiModel):
    id: str
    name: str
    dataset_id: str
    query: DatasetQuery
    comparator: str
    threshold: float
    schedule_cron: str
    schedule_timezone: str
    enabled: bool
    #: ok | tripped | "" (never evaluated) — the stored edge state.
    last_state: str
    #: The last observed value, JSON-encoded; "" before the first evaluation.
    last_value_json: str
    last_dispatched_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


class MonitorCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    dataset_id: str = Field(min_length=1, max_length=36)
    query: DatasetQuery
    comparator: Literal["gt", "lt", "gte", "lte"]
    threshold: float
    schedule_cron: str = Field(min_length=1, max_length=120)
    schedule_timezone: str = Field(default="UTC", max_length=64)


class MonitorUpdateRequest(BaseModel):
    """Every field optional — the PUT sets only what it names.

    `enabled` lives here too, so an enable/disable is a one-field update rather
    than a separate route — the crons router's shape.
    """

    name: Optional[str] = Field(default=None, min_length=1, max_length=160)
    dataset_id: Optional[str] = Field(default=None, min_length=1, max_length=36)
    query: Optional[DatasetQuery] = None
    comparator: Optional[Literal["gt", "lt", "gte", "lte"]] = None
    threshold: Optional[float] = None
    schedule_cron: Optional[str] = Field(default=None, min_length=1, max_length=120)
    schedule_timezone: Optional[str] = Field(default=None, max_length=64)
    enabled: Optional[bool] = None


class MonitorRunNowOut(ApiModel):
    #: ok | tripped | skipped — what the evaluation concluded right now.
    state: str
    #: The observed value, JSON-encoded; "" when skipped.
    value_json: str
    #: Why a skip skipped; "" otherwise.
    reason: str


def _out(monitor: Monitor) -> MonitorOut:
    return MonitorOut(
        id=monitor.id,
        name=monitor.name,
        dataset_id=monitor.dataset_id,
        query=DatasetQuery.model_validate_json(monitor.query_json or "{}"),
        comparator=monitor.comparator,
        threshold=monitor.threshold,
        schedule_cron=monitor.schedule_cron,
        schedule_timezone=monitor.schedule_timezone,
        enabled=monitor.enabled,
        last_state=monitor.last_state,
        last_value_json=monitor.last_value_json,
        last_dispatched_at=monitor.last_dispatched_at,
        created_at=monitor.created_at,
        updated_at=monitor.updated_at,
    )


def _load(db: Session, actor: Actor, monitor_id: str) -> Monitor:
    """Resolve a monitor id inside the caller's workspace, or 404 — never 403,
    which would confirm the id names a real monitor somewhere else."""
    monitor = db.scalar(
        select(Monitor).where(
            Monitor.id == monitor_id,
            Monitor.workspace_id == actor.workspace_id,
        )
    )
    if monitor is None:
        raise HTTPException(status_code=404, detail="Monitor not found")
    return monitor


def _resolve_dataset(db: Session, actor: Actor, dataset_id: str) -> None:
    """The dataset must be the caller's own. Resolved FIRST on create/update so
    a foreign dataset id uniformly 404s before any validation detail leaks."""
    dataset = db.scalar(
        select(Dataset).where(
            Dataset.id == dataset_id,
            Dataset.workspace_id == actor.workspace_id,
        )
    )
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")


def _validate_schedule(schedule_cron: str, timezone: str) -> None:
    """422 a bad cron or IANA zone at the boundary, exactly as a cron is."""
    error = cron_error(schedule_cron)
    if error is not None:
        raise HTTPException(status_code=422, detail=error)
    try:
        ZoneInfo(timezone or "UTC")
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise HTTPException(
            status_code=422, detail=f"unknown timezone “{timezone}”"
        ) from exc


def _validate_query(query: DatasetQuery) -> None:
    """A monitor watches one number: the first metric of the first row. A query
    with no metric has no number to watch, and finding that out inside a silent
    tick would be far worse than a 422 while a person still holds the form."""
    if not query.metrics:
        raise HTTPException(
            status_code=422, detail="A monitor's query needs at least one metric"
        )


@router.get("", response_model=List[MonitorOut])
def list_monitors(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[MonitorOut]:
    rows = list(
        db.scalars(
            select(Monitor)
            .where(Monitor.workspace_id == actor.workspace_id)
            .order_by(Monitor.created_at.desc())
        )
    )
    return [_out(row) for row in rows]


@router.post("", response_model=MonitorOut, status_code=201)
def create_monitor(
    payload: MonitorCreateRequest,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> MonitorOut:
    replay = find_replay(
        db,
        workspace_id=actor.workspace_id,
        operation="monitor.create",
        key=key,
    )
    if replay:
        monitor = db.scalar(
            select(Monitor).where(
                Monitor.id == replay.resource_id,
                Monitor.workspace_id == actor.workspace_id,
            )
        )
        if monitor is None:
            raise replayed_resource_gone()
        return _out(monitor)
    _resolve_dataset(db, actor, payload.dataset_id)
    _validate_schedule(payload.schedule_cron, payload.schedule_timezone)
    _validate_query(payload.query)
    monitor = Monitor(
        workspace_id=actor.workspace_id,
        created_by=actor.user_id,
        name=payload.name,
        dataset_id=payload.dataset_id,
        query_json=payload.query.model_dump_json(),
        comparator=payload.comparator,
        threshold=payload.threshold,
        schedule_cron=payload.schedule_cron,
        schedule_timezone=payload.schedule_timezone or "UTC",
    )
    db.add(monitor)
    db.flush()
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="monitor.create",
        key=key,
        resource_id=monitor.id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="monitor.created",
        resource_type="monitor",
        resource_id=monitor.id,
        detail={
            "name": monitor.name,
            "dataset_id": monitor.dataset_id,
            "schedule_cron": monitor.schedule_cron,
        },
    )
    db.commit()
    return _out(monitor)


@router.put("/{monitor_id}", response_model=MonitorOut)
def update_monitor(
    monitor_id: str,
    payload: MonitorUpdateRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> MonitorOut:
    monitor = _load(db, actor, monitor_id)
    fields = payload.model_dump(exclude_unset=True)
    if "dataset_id" in fields:
        _resolve_dataset(db, actor, fields["dataset_id"])
    # Validate against the *resulting* pair, so changing only the zone is still
    # checked against the stored cron and vice versa.
    if "schedule_cron" in fields or "schedule_timezone" in fields:
        _validate_schedule(
            fields.get("schedule_cron", monitor.schedule_cron),
            fields.get("schedule_timezone", monitor.schedule_timezone),
        )
    if payload.query is not None:
        _validate_query(payload.query)
    # Changing what is watched makes the stored edge state meaningless: reset it
    # to "never evaluated" so the next trip of the NEW question alerts, instead
    # of a stale "tripped" from the old one swallowing it.
    if {"dataset_id", "query", "comparator", "threshold"} & set(fields):
        monitor.last_state = ""
        monitor.last_value_json = ""
    for field, value in fields.items():
        if field == "query":
            monitor.query_json = payload.query.model_dump_json()  # type: ignore[union-attr]
        else:
            setattr(monitor, field, value)
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="monitor.updated",
        resource_type="monitor",
        resource_id=monitor.id,
        detail={"fields": sorted(fields.keys())},
    )
    db.commit()
    return _out(monitor)


@router.delete("/{monitor_id}", status_code=204)
def delete_monitor(
    monitor_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> None:
    monitor = _load(db, actor, monitor_id)
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="monitor.deleted",
        resource_type="monitor",
        resource_id=monitor.id,
        detail={"name": monitor.name},
    )
    db.delete(monitor)
    db.commit()


@router.post("/{monitor_id}/run-now", response_model=MonitorRunNowOut)
def run_monitor_now(
    monitor_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> MonitorRunNowOut:
    """Evaluate the monitor immediately, without touching the atomic claim.

    Claim-free on purpose, like a cron's run-now: the claim makes the ticker
    fire once per minute, and a person pressing the button is asking for one
    more answer regardless of the tick. The edge rule still applies — pressing
    run-now on an already-tripped monitor re-reads the value but writes no
    duplicate alert.
    """
    monitor = _load(db, actor, monitor_id)
    outcome = monitor_service.evaluate(db, monitor, actor_id=actor.user_id)
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="monitor.run_now",
        resource_type="monitor",
        resource_id=monitor.id,
        detail={"state": outcome.state},
    )
    db.commit()
    return MonitorRunNowOut(
        state=outcome.state,
        value_json=outcome.value_json,
        reason=outcome.reason,
    )
