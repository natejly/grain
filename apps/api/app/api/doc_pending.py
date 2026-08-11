"""Parked document edits, keyed to the document they touch.

The agent's approval card lives in chat, so a user reading a document has no
signal that a diff is waiting on them. This exposes the same parked
AgentToolCall rows the chat card renders, resolved to a document id so the
Documents view can show the diff beside the text it would change — and, for an
edit, broken into the hunks a reviewer can take or leave one at a time.
"""
from __future__ import annotations

from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..database import get_db
from ..models import AgentToolCall, Run
from ..schemas import ApiModel
from ..services.agent_loop import open_document
from ..services.artifacts import proposals

router = APIRouter(prefix="/api/documents-pending", tags=["documents"])

DOCUMENT_TOOLS = ("create_document", "edit_document")
MAX_PENDING = 50

#: Above this the review is sent as the all-or-nothing card instead of as
#: segments. A response carrying fifty documents' full text, line by line, is a
#: slower way to say the same thing than the unified diff already on the row.
MAX_REVIEW_LINES = 4_000


class ReviewSegment(ApiModel):
    """One stretch of the document, as it is now and as the proposal wants it.

    `index` is -1 for a stretch the edit does not touch, and otherwise the hunk
    number a reviewer accepts by — the same number
    `POST /api/agent-tool-calls/{id}/decision` takes in `accepted_hunks`.
    """

    index: int
    before: List[str]
    after: List[str]


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
    # The whole document with the proposal laid over it, or empty when there is
    # nothing to review hunk by hunk (a create, an unresolvable target, a `find`
    # that no longer matches, a document too long to ship line by line). An
    # empty list is the signal to fall back to the whole-proposal card.
    segments: List[ReviewSegment]
    created_at: datetime


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
        args = proposals.arguments_of(call.arguments_json)
        run = db.get(Run, call.run_id)
        if run is not None and run.workspace_id != actor.workspace_id:
            run = None
        open_doc = open_document(db, run) if run is not None else None
        document = proposals.target_document(
            db,
            workspace_id=actor.workspace_id,
            name=call.name,
            args=args,
            open_document_id=open_doc.id if open_doc else "",
        )
        segments = proposals.review_segments(document, args)
        # No hunks means the proposal changes nothing after all, which the diff
        # already says better than a document with no decisions in it.
        if not any(segment.changed for segment in segments):
            segments = []
        elif sum(len(segment.before) for segment in segments) > MAX_REVIEW_LINES:
            segments = []
        pending.append(
            PendingDocumentEditOut(
                id=call.id,
                run_id=call.run_id,
                name=call.name,
                document_id=document.id if document else "",
                title=document.title if document else proposals.text_of(args, "title"),
                proposal_preview=call.proposal_preview,
                segments=[
                    ReviewSegment(
                        index=segment.index,
                        before=segment.before,
                        after=segment.after,
                    )
                    for segment in segments
                ],
                created_at=call.created_at,
            )
        )
    return pending
