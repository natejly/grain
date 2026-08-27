from __future__ import annotations

import shutil
from pathlib import Path
from typing import List, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..config import Settings, get_settings
from ..database import get_db
from ..models import Chunk, Source, new_id
from ..schemas import ChunkOut, SourceOut
from ..services import spaces as spaces_service
from ..services.audit import record_audit
from ..services.graph import mark_graph_stale, rebuild_graph
from ..services.ingestion import (
    SOURCE_CONTENT_ROUTE,
    ingest_source,
    object_path,
    purge_source,
    sanitize_filename,
    validate_filename,
)
from ..services.web_search import WEB_CHUNK_PREFIX
from .dependencies import idempotency_key
from .idempotency import find_replay, record_key, replayed_resource_gone
from .ratelimit import rate_limit

router = APIRouter(prefix="/api", tags=["sources"])


@router.get("/sources", response_model=List[SourceOut])
def list_sources(
    space_id: Optional[str] = Query(default=None),
    conversation_id: Optional[str] = Query(default=None),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[Source]:
    stmt = (
        select(Source)
        .where(
            Source.workspace_id == actor.workspace_id,
            Source.deleted_at.is_(None),
        )
        .order_by(Source.created_at.desc())
    )
    if space_id is not None:
        # Exact, "" included, so the library page and a space page are both one
        # request. Only ever narrows; absent means everything, as before.
        stmt = stmt.where(Source.space_id == space_id)
    # The conversation axis defaults the other way round from the space one, and
    # the asymmetry is deliberate. A space's files are still the workspace's
    # files, so an unfiltered call showing them is right. A file attached to one
    # chat is not: listing it in the library is the "it just went into the
    # general knowledge base" outcome the scope exists to prevent, and the
    # Sources page calls this with no arguments. So absent means the library
    # alone, and a caller wanting a thread's files must name the thread.
    stmt = stmt.where(Source.conversation_id == (conversation_id or ""))
    return list(db.scalars(stmt))


@router.post(
    "/sources",
    response_model=SourceOut,
    status_code=202,
    dependencies=[Depends(rate_limit("source-ingest", tier="heavy"))],
)
async def upload_source(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    space_id: str = Form(""),
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> Source:
    replay = find_replay(
        db,
        workspace_id=actor.workspace_id,
        operation="source.upload",
        key=key,
    )
    if replay:
        source = db.scalar(
            select(Source).where(
                Source.id == replay.resource_id,
                Source.workspace_id == actor.workspace_id,
                Source.deleted_at.is_(None),
            )
        )
        if source is None:
            raise replayed_resource_gone()
        return source
    if space_id:
        # Proved against the caller's workspace before it is stamped; a foreign
        # or deleted space is the same fact to this caller — 404 — never a file
        # that silently lands in the workspace library instead.
        try:
            spaces_service.get_space(
                db, workspace_id=actor.workspace_id, space_id=space_id
            )
        except spaces_service.SpaceError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
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
        space_id=space_id,
    )
    path = object_path(actor.workspace_id, source.id, filename)
    path.write_bytes(data)
    source.object_key = str(path)
    db.add(source)
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="source.upload",
        key=key,
        resource_id=source.id,
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


#: Types served with their own Content-Type. Everything else is downloaded as
#: application/octet-stream, because the value here is the browser's instruction
#: for what to *do* with the bytes and the stored media_type is not trustworthy:
#: for an upload it is whatever the client's multipart part claimed.
_SERVED_TYPES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/svg+xml",
    "application/pdf",
    "application/json",
    "text/plain",
    "text/csv",
    "text/markdown",
    "text/html",
}

#: The only types rendered in the browser instead of downloaded. Raster images
#: only, and deliberately not `image/svg+xml` or `text/html`: this API is a
#: different origin from the web app but it is the origin the session cookie
#: belongs to, so an inline document here executes its own script *as the API*.
#: An SVG is a script container, which is exactly why it is on the served list
#: and off this one. A PDF stays an attachment for the same reason — viewers
#: execute embedded JavaScript.
_INLINE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


@router.get(SOURCE_CONTENT_ROUTE)
def get_source_content(
    source_id: str,
    actor: Actor = Depends(get_actor),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> FileResponse:
    """Stream one source's original bytes.

    Every file this workspace holds — an uploaded PDF, a chart a sandbox run
    plotted — has a row, a size and a listing, and until now no way to open it.
    A matplotlib figure the agent draws was stored and then invisible.
    """
    source = db.scalar(
        select(Source).where(
            Source.id == source_id,
            Source.workspace_id == actor.workspace_id,
            Source.deleted_at.is_(None),
        )
    )
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")

    # The stored key is a path this app wrote, but it is still a string in a
    # database: a bad migration, a restored row or a future writer that skips
    # `object_path` could point it anywhere, and the difference between reading
    # this workspace's object and reading /etc/passwd is one such string. Resolve
    # it and require the result to sit under this workspace's own directory —
    # containment is checked against where the bytes must be, not against
    # patterns the key must avoid.
    root = settings.objects_dir.resolve() / actor.workspace_id
    try:
        path = Path(source.object_key).resolve()
        path.relative_to(root)
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="Source file is unavailable") from None
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Source file is unavailable")

    # This route is a viewer, not a bulk export. Everything that writes a Source
    # caps itself at the upload ceiling already, so a file above it means a row
    # that arrived some other way — refuse rather than stream it.
    if path.stat().st_size > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="Source is too large to serve")

    media_type = source.media_type if source.media_type in _SERVED_TYPES else (
        "application/octet-stream"
    )
    return FileResponse(
        path,
        media_type=media_type,
        # Re-sanitised rather than trusted: the header is quoted, and the stored
        # filename should already be safe, but this is the one place it is
        # echoed back to a browser.
        filename=sanitize_filename(source.filename),
        content_disposition_type=(
            "inline" if media_type in _INLINE_TYPES else "attachment"
        ),
    )


@router.get("/chunks/{chunk_id}", response_model=ChunkOut)
def get_chunk(
    chunk_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> ChunkOut:
    if chunk_id.startswith(WEB_CHUNK_PREFIX):
        # A web citation's provenance is the page, and the page is not ours. It
        # is deliberately not synthesised into a ChunkOut here: `content`,
        # `char_start` and `char_end` would all be invented — the excerpt on a
        # web citation is the sentence the *model* wrote that the provider says
        # the page supports, not text quoted from the page — and a provenance
        # viewer that shows fabricated offsets is worse than one that shows
        # nothing. The citation carries `url`; following it is the check.
        raise HTTPException(
            status_code=404,
            detail=(
                "A web citation has no indexed passage. Open its url to check it."
            ),
        )
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
    replay = find_replay(
        db,
        workspace_id=actor.workspace_id,
        operation="source.delete",
        key=key,
    )
    if replay:
        return
    source = purge_source(db, workspace_id=actor.workspace_id, source_id=source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    mark_graph_stale(db, actor.workspace_id)
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="source.delete",
        key=key,
        resource_id=source.id,
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
