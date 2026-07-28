from __future__ import annotations

import json
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..database import get_db
from ..models import IdempotencyRecord, MemoryItem
from ..schemas import MemoryItemOut
from ..services.audit import record_audit
from .dependencies import idempotency_key

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
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


@router.get("/memory", response_model=List[MemoryItemOut])
def list_memory(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[MemoryItemOut]:
    items = db.scalars(
        select(MemoryItem)
        .where(
            MemoryItem.workspace_id == actor.workspace_id,
            MemoryItem.status == "active",
        )
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
    replay = db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.workspace_id == actor.workspace_id,
            IdempotencyRecord.operation == "memory.forget",
            IdempotencyRecord.key == key,
        )
    )
    if replay:
        return
    item = db.scalar(
        select(MemoryItem).where(
            MemoryItem.id == memory_id,
            MemoryItem.workspace_id == actor.workspace_id,
        )
    )
    if item is None or item.status == "deleted":
        raise HTTPException(status_code=404, detail="Memory not found")
    item.status = "deleted"
    db.add(
        IdempotencyRecord(
            workspace_id=actor.workspace_id,
            operation="memory.forget",
            key=key,
            resource_id=item.id,
        )
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
