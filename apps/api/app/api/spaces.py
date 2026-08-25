"""Spaces: the grouping object itself.

Threads join a space at `POST /api/conversations` (api/chat.py), knowledge
files at `POST /api/sources` (api/sources.py); this module only ever handles
the container. Error mapping follows api/folders.py — resolve first so "not
yours" is a 404 everywhere, and a 422 can only mean "that edit is not
allowed".
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Dict, List, Tuple

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..database import get_db
from ..models import Space
from ..schemas import SpaceCreate, SpaceOut, SpaceUpdateRequest
from ..services import spaces
from ..services.audit import record_audit
from ..services.graph import mark_graph_stale, rebuild_graph
from .dependencies import idempotency_key
from .idempotency import find_replay, record_key, replayed_resource_gone

router = APIRouter(prefix="/api", tags=["spaces"])


def _out(space: Space, counts: Dict[str, Tuple[int, int]]) -> SpaceOut:
    threads, sources = counts.get(space.id, (0, 0))
    return SpaceOut(
        id=space.id,
        name=space.name,
        instructions=space.instructions,
        thread_count=threads,
        source_count=sources,
        created_at=space.created_at,
        updated_at=space.updated_at,
    )


@router.get("/spaces", response_model=List[SpaceOut])
def list_spaces(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[SpaceOut]:
    counts = spaces.counts_for(db, workspace_id=actor.workspace_id)
    return [
        _out(space, counts)
        for space in spaces.list_spaces(db, workspace_id=actor.workspace_id)
    ]


@router.post("/spaces", response_model=SpaceOut, status_code=201)
def create_space(
    payload: SpaceCreate,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> SpaceOut:
    replay = find_replay(
        db, workspace_id=actor.workspace_id, operation="space.create", key=key
    )
    if replay:
        space = db.get(Space, replay.resource_id)
        if space is None or space.workspace_id != actor.workspace_id:
            raise replayed_resource_gone()
        return _out(space, spaces.counts_for(db, workspace_id=actor.workspace_id))
    try:
        space = spaces.create_space(
            db,
            workspace_id=actor.workspace_id,
            name=payload.name,
            instructions=payload.instructions,
            created_by=actor.user_id,
        )
    except spaces.SpaceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="space.create",
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
        detail={"name": space.name},
    )
    db.commit()
    db.refresh(space)
    return _out(space, {})


@router.get("/spaces/{space_id}", response_model=SpaceOut)
def get_space(
    space_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> SpaceOut:
    try:
        space = spaces.get_space(
            db, workspace_id=actor.workspace_id, space_id=space_id
        )
    except spaces.SpaceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _out(space, spaces.counts_for(db, workspace_id=actor.workspace_id))


@router.patch("/spaces/{space_id}", response_model=SpaceOut)
def update_space(
    space_id: str,
    payload: SpaceUpdateRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> SpaceOut:
    # Resolved first so "not yours" is a 404 like every route that names a
    # space, and the 422 below can only ever mean "that edit is not allowed".
    try:
        spaces.get_space(db, workspace_id=actor.workspace_id, space_id=space_id)
    except spaces.SpaceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        space = spaces.update_space(
            db,
            workspace_id=actor.workspace_id,
            space_id=space_id,
            name=payload.name,
            instructions=payload.instructions,
        )
    except spaces.SpaceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="space.updated",
        resource_type="space",
        resource_id=space.id,
        detail={"name": space.name},
    )
    db.commit()
    db.refresh(space)
    return _out(space, spaces.counts_for(db, workspace_id=actor.workspace_id))


@router.delete("/spaces/{space_id}", status_code=204)
def delete_space(
    space_id: str,
    background_tasks: BackgroundTasks,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> None:
    replay = find_replay(
        db, workspace_id=actor.workspace_id, operation="space.delete", key=key
    )
    if replay:
        return
    try:
        teardown = spaces.delete_space(
            db, workspace_id=actor.workspace_id, space_id=space_id
        )
    except spaces.SpaceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if teardown.source_count:
        mark_graph_stale(db, actor.workspace_id)
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="space.delete",
        key=key,
        resource_id=space_id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="space.deleted",
        resource_type="space",
        resource_id=space_id,
        detail={
            "threads": teardown.thread_count,
            "sources": teardown.source_count,
            "memories": teardown.memory_count,
        },
    )
    db.commit()
    if teardown.source_count:
        background_tasks.add_task(rebuild_graph, actor.workspace_id, actor.user_id)
    # Disk only after the commit has held, mirroring DELETE /api/sources/{id}:
    # bytes for rows that still exist are recoverable, rows for bytes that are
    # gone are not.
    for object_key in teardown.object_keys:
        object_file = Path(object_key)
        try:
            if object_file.exists():
                object_file.unlink()
            parent = object_file.parent
            if parent.exists():
                shutil.rmtree(parent)
        except OSError:
            pass
