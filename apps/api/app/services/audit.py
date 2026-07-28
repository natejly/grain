from __future__ import annotations

import json
from typing import Any, Dict

from sqlalchemy.orm import Session

from ..models import AuditEvent


def record_audit(
    db: Session,
    *,
    workspace_id: str,
    actor_id: str,
    action: str,
    resource_type: str,
    resource_id: str,
    detail: Dict[str, Any],
) -> AuditEvent:
    event = AuditEvent(
        workspace_id=workspace_id,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        detail_json=json.dumps(detail, separators=(",", ":"), default=str),
    )
    db.add(event)
    return event

