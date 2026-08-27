"""Attaching a file to a conversation, listing what is attached, detaching it.

One upload endpoint rather than two, because which destination a file gets is
not the caller's decision to make: `services/attachments` routes text to an
editable Document and everything else to a conversation-scoped Source, and a
client that could choose would be a second place for that rule to live. The
response says what the file became (`kind`), which is what the chip needs in
order to know whether the file can be opened in the editor.
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..config import Settings, get_settings
from ..database import get_db
from ..models import ChatAttachment, Source, new_id
from ..schemas import ChatAttachmentOut
from ..services import attachments as attachments_service
from ..services.audit import record_audit
from ..services.ingestion import (
    ingest_source,
    object_path,
    sanitize_filename,
    validate_filename,
)
from .ratelimit import rate_limit

router = APIRouter(prefix="/api", tags=["attachments"])


def _conversation(
    db: Session, actor: Actor, conversation_id: str
) -> None:
    try:
        attachments_service.get_conversation(
            db, workspace_id=actor.workspace_id, conversation_id=conversation_id
        )
    except attachments_service.AttachmentError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/conversations/{conversation_id}/attachments",
    response_model=List[ChatAttachmentOut],
)
def list_attachments(
    conversation_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[ChatAttachment]:
    _conversation(db, actor, conversation_id)
    return attachments_service.list_for_conversation(
        db, workspace_id=actor.workspace_id, conversation_id=conversation_id
    )


@router.post(
    "/conversations/{conversation_id}/attachments",
    response_model=ChatAttachmentOut,
    status_code=201,
    dependencies=[Depends(rate_limit("attachment-upload", tier="heavy"))],
)
async def create_attachment(
    conversation_id: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    actor: Actor = Depends(get_actor),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> ChatAttachment:
    _conversation(db, actor, conversation_id)
    filename = sanitize_filename(file.filename or "attachment.txt")
    try:
        validate_filename(filename)
    except ValueError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    # Same ceiling as a library upload, read one byte past it so an oversized
    # file is refused rather than silently truncated.
    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="File exceeds the 10 MB limit")
    if not data:
        raise HTTPException(status_code=400, detail="File is empty")

    if attachments_service.is_text(filename):
        try:
            attachment = attachments_service.attach_document(
                db,
                workspace_id=actor.workspace_id,
                conversation_id=conversation_id,
                filename=filename,
                data=data,
                created_by=actor.user_id,
            )
        except attachments_service.AttachmentError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    else:
        source = Source(
            id=new_id(),
            workspace_id=actor.workspace_id,
            created_by=actor.user_id,
            filename=filename,
            media_type=file.content_type or "application/octet-stream",
            object_key="",
            byte_size=len(data),
            status="queued",
            # The scope, and the whole point: these passages are retrievable
            # from this thread and from no other. See `retrieval._live_sources`.
            conversation_id=conversation_id,
        )
        path = object_path(actor.workspace_id, source.id, filename)
        path.write_bytes(data)
        source.object_key = str(path)
        db.add(source)
        attachment = attachments_service.record(
            db,
            workspace_id=actor.workspace_id,
            conversation_id=conversation_id,
            kind=attachments_service.SOURCE,
            target_id=source.id,
            filename=filename,
            created_by=actor.user_id,
        )
        background_tasks.add_task(ingest_source, source.id, actor.user_id)

    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="attachment.created",
        resource_type="chat_attachment",
        resource_id=attachment.id,
        detail={
            "filename": filename,
            "bytes": len(data),
            "kind": attachment.kind,
            "conversation_id": conversation_id,
        },
    )
    db.commit()
    db.refresh(attachment)
    return attachment


@router.delete("/attachments/{attachment_id}", status_code=204)
def delete_attachment(
    attachment_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> None:
    try:
        attachment = attachments_service.get(
            db, workspace_id=actor.workspace_id, attachment_id=attachment_id
        )
    except attachments_service.AttachmentError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # Read off the row before it is deleted: `detach` commits, and a deleted
    # instance cannot be asked what it used to say.
    detail = {"filename": attachment.filename, "kind": attachment.kind}
    attachments_service.detach(
        db, workspace_id=actor.workspace_id, attachment_id=attachment_id
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="attachment.removed",
        resource_type="chat_attachment",
        resource_id=attachment_id,
        detail=detail,
    )
    db.commit()
