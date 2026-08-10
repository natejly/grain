"""Parked document edits, keyed to the document they touch.

The agent's approval card lives in chat, so a user reading a document has no
signal that a diff is waiting on them. This exposes the same parked
AgentToolCall rows the chat card renders, resolved to a document id so the
Documents view can show the diff beside the text it would change.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..database import get_db
from ..models import AgentToolCall, Document, Run
from ..schemas import ApiModel
from ..services.artifacts import documents

router = APIRouter(prefix="/api/documents-pending", tags=["documents"])

DOCUMENT_TOOLS = ("create_document", "edit_document")
MAX_PENDING = 50


class PendingDocumentEditOut(ApiModel):
    """One proposed document write awaiting a decision."""

    id: str
    run_id: str
    name: str
    # Empty when the target could not be resolved — a create has no document
    # yet, and a model-supplied id may be a hallucination.
    document_id: str
    title: str
    proposal_preview: str
    created_at: datetime


def _arguments(raw: str) -> Dict[str, Any]:
    """Model-generated JSON. Truncation and hallucinated shapes are routine, so
    anything that is not an object decodes to no arguments at all."""
    try:
        parsed = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _text(args: Dict[str, Any], key: str) -> str:
    value = args.get(key)
    return value.strip() if isinstance(value, str) else ""


def _target(
    db: Session, *, workspace_id: str, name: str, args: Dict[str, Any]
) -> Optional[Document]:
    """The document this call would change, if it exists in this workspace."""
    if name == "create_document":
        return None
    try:
        return documents.resolve(
            db,
            workspace_id=workspace_id,
            document_id=_text(args, "document_id"),
            title=_text(args, "title"),
        )
    except documents.DocumentError:
        return None


@router.get("", response_model=List[PendingDocumentEditOut])
def list_pending_document_edits(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[PendingDocumentEditOut]:
    """Proposed create/edit calls whose run is still parked on the decision.

    Runs that were cancelled leave their proposal rows behind; those can no
    longer be approved, so they are filtered out rather than offered as a
    button that would 409.
    """
    calls = db.scalars(
        select(AgentToolCall)
        .join(Run, Run.id == AgentToolCall.run_id)
        .where(
            AgentToolCall.workspace_id == actor.workspace_id,
            AgentToolCall.status == "proposed",
            AgentToolCall.name.in_(DOCUMENT_TOOLS),
            Run.status == "waiting_for_approval",
        )
        .order_by(AgentToolCall.created_at.desc())
        .limit(MAX_PENDING)
    )
    pending: List[PendingDocumentEditOut] = []
    for call in calls:
        args = _arguments(call.arguments_json)
        document = _target(
            db, workspace_id=actor.workspace_id, name=call.name, args=args
        )
        pending.append(
            PendingDocumentEditOut(
                id=call.id,
                run_id=call.run_id,
                name=call.name,
                document_id=document.id if document else "",
                title=document.title if document else _text(args, "title"),
                proposal_preview=call.proposal_preview,
                created_at=call.created_at,
            )
        )
    return pending
