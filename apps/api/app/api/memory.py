from __future__ import annotations

import json
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..database import get_db
from ..models import MemoryItem
from ..schemas import MemoryItemOut
from ..services.audit import record_audit
from ..services.memory import SHARED_OWNER, _active, tombstone_key
from .dependencies import idempotency_key
from .idempotency import find_replay, record_key

router = APIRouter(prefix="/api", tags=["memory"])


def _memory_out(item: MemoryItem) -> MemoryItemOut:
    return MemoryItemOut(
        id=item.id,
        conversation_id=item.conversation_id,
        kind=item.kind,
        content=item.content,
        entity_names=json.loads(item.entity_names_json),
        message_ids=json.loads(item.message_ids_json),
        importance=item.importance,
        # A boolean and not the owner id: the caller only ever receives shared
        # rows and their own, so "is this everyone's" is the whole of what they
        # can learn, and putting a user id on the wire would say more. Same shape
        # `ConversationOut.shared` settled on for the identical question.
        shared=item.owner_id == SHARED_OWNER,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


@router.get("/memory", response_model=List[MemoryItemOut])
def list_memory(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[MemoryItemOut]:
    """The workspace's memories and the caller's own — never another member's.

    Which means nobody, owner included, has a complete view of what the workspace
    knows. ADR 0010 records that as a real cost rather than an oversight: it is
    the same trade `Conversation.shared` already made, and the audit trail still
    records every write.
    """
    items = db.scalars(
        _active(select(MemoryItem), actor.workspace_id, actor.user_id)
        .order_by(MemoryItem.updated_at.desc())
        .limit(200)
    )
    return [_memory_out(item) for item in items]


@router.delete("/memory/{memory_id}", status_code=204)
def forget_memory(
    memory_id: str,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> None:
    replay = find_replay(
        db,
        workspace_id=actor.workspace_id,
        operation="memory.forget",
        key=key,
    )
    if replay:
        return
    # `_active` rather than a status check of its own, so forgetting reaches
    # exactly the rows listing shows: shared plus the caller's own. Another
    # member's personal memory is a 404 here for the same reason it is invisible
    # above — it is not that you may not delete it, it is that it is not yours.
    item = db.scalar(
        _active(select(MemoryItem), actor.workspace_id, actor.user_id).where(
            MemoryItem.id == memory_id
        )
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    # Same tombstone as the agent's `forget` tool, so this endpoint cannot leave
    # a deleted row parked on a claim key and make that claim unlearnable.
    item.status = "deleted"
    item.normalized_key = tombstone_key(db, item)
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="memory.forget",
        key=key,
        resource_id=item.id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="memory.forgotten",
        resource_type="memory_item",
        resource_id=item.id,
        detail={"kind": item.kind},
    )
    db.commit()
