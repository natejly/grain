from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..config import Settings, get_settings
from ..database import get_db
from ..models import Chunk, IdempotencyRecord, Source, new_id
from ..schemas import ChunkOut, SourceOut
from ..services.audit import record_audit
from ..services.graph import mark_graph_stale, rebuild_graph
from ..services.ingestion import (
    ingest_source,
    object_path,
    sanitize_filename,
    validate_filename,
)
from .dependencies import idempotency_key

router = APIRouter(prefix="/api", tags=["sources"])


@router.get("/sources", response_model=List[SourceOut])
def list_sources(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[Source]:
    return list(
        db.scalars(
            select(Source)
            .where(
                Source.workspace_id == actor.workspace_id,
                Source.deleted_at.is_(None),
            )
            .order_by(Source.created_at.desc())
        )
    )


@router.post("/sources", response_model=SourceOut, status_code=202)
async def upload_source(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> Source:
    replay = db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.workspace_id == actor.workspace_id,
            IdempotencyRecord.operation == "source.upload",
            IdempotencyRecord.key == key,
        )
    )
    if replay:
        source = db.scalar(
            select(Source).where(
                Source.id == replay.resource_id,
                Source.workspace_id == actor.workspace_id,
                Source.deleted_at.is_(None),
            )
        )
        if source:
            return source
    filename = sanitize_filename(file.filename or "source.txt")
    try:
        validate_filename(filename)
    except ValueError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="Source exceeds the 10 MB limit")
    if not data:
        raise HTTPException(status_code=400, detail="Source is empty")
    source = Source(
        id=new_id(),
        workspace_id=actor.workspace_id,
        created_by=actor.user_id,
        filename=filename,
        media_type=file.content_type or "application/octet-stream",
        object_key="",
        byte_size=len(data),
        status="queued",
    )
    path = object_path(actor.workspace_id, source.id, filename)
    path.write_bytes(data)
    source.object_key = str(path)
    db.add(source)
    db.add(
        IdempotencyRecord(
            workspace_id=actor.workspace_id,
            operation="source.upload",
            key=key,
            resource_id=source.id,
        )
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="source.uploaded",
        resource_type="source",
        resource_id=source.id,
        detail={"filename": filename, "bytes": len(data)},
    )
    db.commit()
    background_tasks.add_task(ingest_source, source.id, actor.user_id)
    return source


@router.get("/chunks/{chunk_id}", response_model=ChunkOut)
def get_chunk(
    chunk_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> ChunkOut:
    row = db.execute(
        select(Chunk, Source)
        .join(Source, Source.id == Chunk.source_id)
        .where(
            Chunk.id == chunk_id,
            Chunk.workspace_id == actor.workspace_id,
            Source.workspace_id == actor.workspace_id,
            Source.deleted_at.is_(None),
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Provenance passage not found")
    chunk, source = row
    return ChunkOut(
        id=chunk.id,
        source_id=source.id,
        ordinal=chunk.ordinal,
        content=chunk.content,
        char_start=chunk.char_start,
        char_end=chunk.char_end,
        filename=source.filename,
    )


@router.delete("/sources/{source_id}", status_code=204)
def delete_source(
    source_id: str,
    background_tasks: BackgroundTasks,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> None:
    replay = db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.workspace_id == actor.workspace_id,
            IdempotencyRecord.operation == "source.delete",
            IdempotencyRecord.key == key,
        )
    )
    if replay:
        return
    source = db.scalar(
        select(Source).where(
            Source.id == source_id,
            Source.workspace_id == actor.workspace_id,
            Source.deleted_at.is_(None),
        )
    )
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    source.deleted_at = datetime.utcnow()
    source.status = "deleted"
    db.execute(delete(Chunk).where(Chunk.source_id == source.id))
    mark_graph_stale(db, actor.workspace_id)
    db.add(
        IdempotencyRecord(
            workspace_id=actor.workspace_id,
            operation="source.delete",
            key=key,
            resource_id=source.id,
        )
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="source.deleted",
        resource_type="source",
        resource_id=source.id,
        detail={"filename": source.filename},
    )
    db.commit()
    background_tasks.add_task(rebuild_graph, actor.workspace_id, actor.user_id)
    object_file = Path(source.object_key)
    try:
        if object_file.exists():
            object_file.unlink()
        parent = object_file.parent
        if parent.exists():
            shutil.rmtree(parent)
    except OSError:
        pass
