from __future__ import annotations

import json
from typing import Any, Dict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import RunEvent


def append_event(
    db: Session,
    *,
    workspace_id: str,
    run_id: str,
    event_type: str,
    payload: Dict[str, Any],
) -> RunEvent:
    latest = db.scalar(
        select(func.max(RunEvent.sequence)).where(RunEvent.run_id == run_id)
    )
    event = RunEvent(
        workspace_id=workspace_id,
        run_id=run_id,
        sequence=(latest or 0) + 1,
        event_type=event_type,
        payload_json=json.dumps(payload, separators=(",", ":"), default=str),
    )
    db.add(event)
    db.flush()
    return event

