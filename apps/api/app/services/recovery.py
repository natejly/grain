from __future__ import annotations

from datetime import datetime
from typing import List, Tuple

from sqlalchemy import or_, select

from ..database import SessionLocal
from ..models import Run, Source, ToolCall
from .events import append_event
from .ingestion import ingest_source
from .runs import execute_tool_call, process_run


def recover_durable_work() -> Tuple[int, int, int]:
    """Recover locally executable work after an unclean process stop.

    Production queue adapters should call the same task entrypoints after claiming
    the corresponding durable records with their transport-level lease.
    """

    db = SessionLocal()
    run_ids: List[str] = []
    source_jobs: List[Tuple[str, str]] = []
    tool_call_ids: List[str] = []
    try:
        now = datetime.utcnow()
        runs = list(
            db.scalars(
                select(Run).where(
                    or_(
                        Run.status == "queued",
                        (
                            (Run.status == "running")
                            & (
                                Run.lease_expires_at.is_(None)
                                | (Run.lease_expires_at < now)
                            )
                        ),
                    )
                )
            )
        )
        for run in runs:
            if run.status == "running":
                run.status = "queued"
                run.lease_expires_at = None
                append_event(
                    db,
                    workspace_id=run.workspace_id,
                    run_id=run.id,
                    event_type="run.recovered",
                    payload={"status": "queued"},
                )
            run_ids.append(run.id)

        sources = list(
            db.scalars(
                select(Source).where(
                    Source.deleted_at.is_(None),
                    Source.status.in_(["queued", "processing"]),
                )
            )
        )
        source_jobs = [(source.id, source.created_by) for source in sources]

        tool_calls = list(
            db.scalars(
                select(ToolCall).where(ToolCall.status.in_(["approved", "executing"]))
            )
        )
        for call in tool_calls:
            if call.status == "executing":
                call.status = "approved"
            tool_call_ids.append(call.id)
        db.commit()
    finally:
        db.close()

    for source_id, actor_id in source_jobs:
        ingest_source(source_id, actor_id)
    for tool_call_id in tool_call_ids:
        execute_tool_call(tool_call_id)
    for run_id in run_ids:
        process_run(run_id)
    return len(run_ids), len(source_jobs), len(tool_call_ids)
