from __future__ import annotations

import json
from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..database import get_db
from ..models import AuditEvent
from ..schemas import AuditEventOut

router = APIRouter(prefix="/api", tags=["audit"])


@router.get("/audit-events", response_model=List[AuditEventOut])
def list_audit_events(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[AuditEventOut]:
    events = db.scalars(
        select(AuditEvent)
        .where(AuditEvent.workspace_id == actor.workspace_id)
        .order_by(AuditEvent.created_at.desc())
        .limit(100)
    )
    return [
        AuditEventOut(
            id=event.id,
            action=event.action,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            detail=json.loads(event.detail_json),
            created_at=event.created_at,
        )
        for event in events
    ]

