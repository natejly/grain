"""REST surface over watches.

`api/crons.py`'s construction throughout: `_load` resolves an id inside the
caller's workspace or 404s, every downstream query filters on `workspace_id`
again, the schedule is validated at the boundary so a refusal lands while a
person still holds the form, and dispatch lives in the shared
`POST /api/workflows/tick` rather than in a scheduler of its own.

Two fields carry the whole of this router's judgement:

`shared` is the ONLY control in this cluster that widens memory visibility, so
it is an explicit request field on a workspace-resolved row and is never
inferred from the target. Its helper text on the web side says what it does in
the words the server means. Patching it RETIRES the claims the watch wrote in
the scope it is leaving: supersession matches owner and space exactly, so rows
left in the old scope could never be corrected by a later fire.

`space_id` is DERIVED SERVER-SIDE and never read off the request body — the
target's own space for a source, the target itself for a space. A body-supplied
scope is exactly how a '' sentinel gets written unintentionally, and '' is the
global shelf.

`target` is not patchable. Changing it would silently redefine every memory
claim key the watch has ever written, leaving a shelf of facts attributed to a
question nobody asked.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Dict, List, Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..config import Settings, get_settings
from ..database import get_db
from ..models import Source, Space, Watch, WatchObservation
from ..schemas import ApiModel
from ..services import watches as service
from ..services.audit import record_audit
from ..services.workflows.validate import cron_error
from .dependencies import idempotency_key
from .idempotency import find_replay, record_key, replayed_resource_gone

router = APIRouter(prefix="/api/watches", tags=["watches"])

#: At most ten declared fields. The memory row count is bounded by fields, not
#: by fires (one claim key per (watch, field)), so this cap is the whole of the
#: supersession-churn budget.
MAX_EXTRACTION_FIELDS = 10


class WatchOut(ApiModel):
    id: str
    name: str
    #: "source" | "space".
    target_kind: str
    target_id: str
    #: Derived server-side. '' is the workspace library, never NULL.
    space_id: str
    #: True means this watch writes what it learns to EVERY member's memory
    #: shelf. False means it writes to its creator's.
    shared: bool
    schedule_cron: str
    schedule_timezone: str
    enabled: bool
    extraction_fields: List[str] = []
    #: The standing brief this watch regenerates. '' until the first change.
    brief_document_id: str
    #: None means the watch has never run — a different fact from "ran and
    #: found nothing", which is a checked-at with no change-at.
    last_checked_at: Optional[datetime]
    last_change_at: Optional[datetime]
    created_by: str
    created_at: datetime


class WatchObservationOut(ApiModel):
    id: str
    #: False is a real observation: the watch ran and the target was identical.
    changed: bool
    added_chunks: int
    removed_chunks: int
    summary: str
    fields: Dict[str, str] = {}
    created_at: datetime


class WatchCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    target_kind: Literal["source", "space"]
    target_id: str = Field(min_length=1, max_length=36)
    schedule_cron: str = Field(min_length=1, max_length=120)
    schedule_timezone: str = Field(default="UTC", max_length=64)
    shared: bool = False
    extraction_fields: List[str] = []


class WatchUpdateRequest(BaseModel):
    """Every field optional — a PATCH sets only what it names.

    `target_kind`/`target_id` are deliberately absent: see the module docstring.
    """

    name: Optional[str] = Field(default=None, min_length=1, max_length=160)
    schedule_cron: Optional[str] = Field(default=None, min_length=1, max_length=120)
    schedule_timezone: Optional[str] = Field(default=None, max_length=64)
    enabled: Optional[bool] = None
    shared: Optional[bool] = None
    extraction_fields: Optional[List[str]] = None


class WatchRunNowOut(ApiModel):
    #: The observation this fire wrote. Always one, changed or not.
    observation: WatchObservationOut


def _fields(raw: str) -> List[str]:
    try:
        parsed = json.loads(raw or "[]")
    except ValueError:
        return []
    return [item for item in parsed if isinstance(item, str)]


def _out(watch: Watch) -> WatchOut:
    return WatchOut(
        id=watch.id,
        name=watch.name,
        target_kind=watch.target_kind,
        target_id=watch.target_id,
        space_id=watch.space_id,
        shared=watch.shared,
        schedule_cron=watch.schedule_cron,
        schedule_timezone=watch.schedule_timezone,
        enabled=watch.enabled,
        extraction_fields=_fields(watch.extraction_schema_json),
        brief_document_id=watch.brief_document_id,
        last_checked_at=watch.last_checked_at,
        last_change_at=watch.last_change_at,
        created_by=watch.created_by,
        created_at=watch.created_at,
    )


def _observation_out(observation: WatchObservation) -> WatchObservationOut:
    try:
        fields = json.loads(observation.fields_json or "{}")
    except ValueError:
        fields = {}
    return WatchObservationOut(
        id=observation.id,
        changed=observation.changed,
        added_chunks=observation.added_chunks,
        removed_chunks=observation.removed_chunks,
        summary=observation.summary,
        fields={
            str(key): str(value)
            for key, value in (fields.items() if isinstance(fields, dict) else [])
        },
        created_at=observation.created_at,
    )


def _load(db: Session, actor: Actor, watch_id: str) -> Watch:
    """Resolve a watch inside the caller's workspace, or 404 (never 403)."""
    watch = db.scalar(
        select(Watch).where(
            Watch.id == watch_id, Watch.workspace_id == actor.workspace_id
        )
    )
    if watch is None:
        raise HTTPException(status_code=404, detail="Watch not found")
    return watch


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
    if _fires_more_often_than_hourly(schedule_cron):
        # Every fire of a CHANGED watch writes one memory row per declared
        # field. Hourly is the finest cadence at which that supersession churn
        # stays legible, and a document nobody edits per-minute gains nothing
        # from being asked per-minute.
        raise HTTPException(
            status_code=422,
            detail="a watch may run at most once an hour — set a fixed minute",
        )


def _fires_more_often_than_hourly(expression: str) -> bool:
    """True when the minute field names more than one minute."""
    parts = expression.split()
    if len(parts) != 5:
        return False
    minute = parts[0]
    if minute == "*" or minute.startswith("*/") or "," in minute or "-" in minute:
        return True
    return False


def _normalized_fields(values: List[str]) -> List[str]:
    """At most ten fields, trimmed, deduplicated casefolded, order preserved.

    A field whose name will not slugify is refused HERE. `watches.claim_key` is
    total, so such a field still supersedes correctly — but only under a digest
    key nobody can read on the memory shelf or in an audit row, and a person
    typing "price (USD)" should learn that while they still hold the form.
    """
    seen: set[str] = set()
    fields: List[str] = []
    for value in values:
        text = value.strip()[:40]
        if not text:
            continue
        if not service.slugifies(text):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"“{text}” cannot be used as a field name — letters, digits, "
                    "spaces and - . : / only"
                ),
            )
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        fields.append(text)
    if len(fields) > MAX_EXTRACTION_FIELDS:
        raise HTTPException(
            status_code=422,
            detail=f"a watch may declare at most {MAX_EXTRACTION_FIELDS} fields",
        )
    return fields


def _resolve_target(
    db: Session, *, actor: Actor, target_kind: str, target_id: str
) -> str:
    """Prove the target is the caller's, and DERIVE the watch's space from it.

    A foreign source or space 404s here, before anything else can answer — and
    the space this watch writes memory into is read off the resolved row, never
    off the request.
    """
    if target_kind == "space":
        found = db.scalar(
            select(Space.id).where(
                Space.id == target_id, Space.workspace_id == actor.workspace_id
            )
        )
        if found is None:
            raise HTTPException(status_code=404, detail="Space not found")
        return str(found)
    source = db.scalar(
        select(Source).where(
            Source.id == target_id,
            Source.workspace_id == actor.workspace_id,
            Source.deleted_at.is_(None),
        )
    )
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return source.space_id


@router.get("", response_model=List[WatchOut])
def list_watches(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[WatchOut]:
    rows = list(
        db.scalars(
            select(Watch)
            .where(Watch.workspace_id == actor.workspace_id)
            .order_by(Watch.created_at.desc(), Watch.id)
        )
    )
    return [_out(watch) for watch in rows]


@router.post("", response_model=WatchOut, status_code=201)
def create_watch(
    payload: WatchCreateRequest,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> WatchOut:
    replay = find_replay(
        db, workspace_id=actor.workspace_id, operation="watch.create", key=key
    )
    if replay:
        existing = db.scalar(
            select(Watch).where(
                Watch.id == replay.resource_id,
                Watch.workspace_id == actor.workspace_id,
            )
        )
        if existing is None:
            raise replayed_resource_gone()
        return _out(existing)
    space_id = _resolve_target(
        db,
        actor=actor,
        target_kind=payload.target_kind,
        target_id=payload.target_id,
    )
    _validate_schedule(payload.schedule_cron, payload.schedule_timezone)
    fields = _normalized_fields(payload.extraction_fields)
    watch = Watch(
        workspace_id=actor.workspace_id,
        created_by=actor.user_id,
        name=payload.name,
        target_kind=payload.target_kind,
        target_id=payload.target_id,
        space_id=space_id,
        shared=payload.shared,
        schedule_cron=payload.schedule_cron,
        schedule_timezone=payload.schedule_timezone or "UTC",
        extraction_schema_json=json.dumps(fields),
    )
    db.add(watch)
    db.flush()
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="watch.create",
        key=key,
        resource_id=watch.id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="watch.created",
        resource_type="watch",
        resource_id=watch.id,
        detail={
            "target_kind": watch.target_kind,
            "target_id": watch.target_id,
            "shared": watch.shared,
            "schedule_cron": watch.schedule_cron,
            "fields": fields,
        },
    )
    db.commit()
    return _out(watch)


@router.get("/{watch_id}", response_model=WatchOut)
def get_watch(
    watch_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> WatchOut:
    return _out(_load(db, actor, watch_id))


@router.patch("/{watch_id}", response_model=WatchOut)
def update_watch(
    watch_id: str,
    payload: WatchUpdateRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> WatchOut:
    watch = _load(db, actor, watch_id)
    fields = payload.model_dump(exclude_unset=True)
    if "schedule_cron" in fields or "schedule_timezone" in fields:
        _validate_schedule(
            fields.get("schedule_cron", watch.schedule_cron),
            fields.get("schedule_timezone", watch.schedule_timezone),
        )
    declared = fields.pop("extraction_fields", None)
    # THE FLIP RECONCILES THE SCOPE IT IS LEAVING. `shared` decides which shelf
    # this watch writes to, and supersession matches owner AND space exactly —
    # so the rows written under the old owner become unreachable the moment the
    # flag changes, and no future fire can ever correct them. Retired BEFORE the
    # setattr, while `watch.shared` still names the scope being left. (The
    # module docstring makes this argument for `target`, which is simply not
    # patchable; `shared` is, so it pays the cost instead.)
    retired = 0
    old_owner = ""
    if "shared" in fields and bool(fields["shared"]) != bool(watch.shared):
        old_owner = "" if watch.shared else watch.created_by
        retired = service.retire_claims(db, watch=watch, owner_id=old_owner)
    if declared is not None:
        watch.extraction_schema_json = json.dumps(_normalized_fields(declared))
    for name, value in fields.items():
        setattr(watch, name, value)
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="watch.updated",
        resource_type="watch",
        resource_id=watch.id,
        detail={
            "fields": sorted(
                list(fields)
                + (["extraction_fields"] if declared is not None else [])
            ),
            "shared": watch.shared,
            # A widening or narrowing of memory visibility is the one thing in
            # this router worth being able to reconstruct later.
            "retired_claims": retired,
            "retired_scope": old_owner,
        },
    )
    db.commit()
    return _out(watch)


@router.delete("/{watch_id}", status_code=204)
def delete_watch(
    watch_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> None:
    """Delete the watch and its observations. KEEP the brief, KEEP the memories.

    The brief is a Document a person may have edited, and it is not the watch's
    to delete. The memory rows are the workspace's knowledge: they are retired
    by supersession, by a later claim on the same key, not by the question that
    produced them going away.
    """
    watch = _load(db, actor, watch_id)
    for observation in db.scalars(
        select(WatchObservation).where(
            WatchObservation.watch_id == watch.id,
            WatchObservation.workspace_id == actor.workspace_id,
        )
    ):
        db.delete(observation)
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="watch.deleted",
        resource_type="watch",
        resource_id=watch.id,
        detail={
            "target_kind": watch.target_kind,
            "brief_document_id": watch.brief_document_id,
        },
    )
    db.delete(watch)
    db.commit()


@router.post("/{watch_id}/run-now", response_model=WatchRunNowOut, status_code=202)
def run_watch_now(
    watch_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> WatchRunNowOut:
    """Check the watch immediately, without touching the atomic claim.

    Claim-free on purpose, the cron run-now precedent: the claim exists to make
    the unattended ticker fire once a minute, and a person pressing the button
    is asking for exactly one more check regardless of when the last tick was.
    """
    watch = _load(db, actor, watch_id)
    observation = service.check(db, watch=watch, settings=settings)
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="watch.run_now",
        resource_type="watch",
        resource_id=watch.id,
        detail={"changed": observation.changed},
    )
    db.commit()
    return WatchRunNowOut(observation=_observation_out(observation))


@router.get("/{watch_id}/observations", response_model=List[WatchObservationOut])
def list_watch_observations(
    watch_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[WatchObservationOut]:
    watch = _load(db, actor, watch_id)
    rows = list(
        db.scalars(
            select(WatchObservation)
            .where(
                WatchObservation.watch_id == watch.id,
                WatchObservation.workspace_id == actor.workspace_id,
            )
            .order_by(
                WatchObservation.created_at.desc(), WatchObservation.id.desc()
            )
            .limit(50)
        )
    )
    return [_observation_out(row) for row in rows]
