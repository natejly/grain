"""Conversations, and the two things that own one.

A thread is normally its own thing: the user makes it, names it, deletes it. A
thread opened from the chat panel beside a document is not — it exists because
the document does, it is handed the document's text on every turn, and when the
document goes it has nothing left to be about. That asymmetry is why the
get-or-create and the cascade live here rather than inline in a route: both the
Chat router and the Documents router act on it, and a cascade implemented twice
is a cascade that will disagree with itself.
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import AgentToolCall, Conversation, Message, Run, RunEvent, ToolCall


def for_document(
    db: Session, *, workspace_id: str, document_id: str, user_id: str, title: str
) -> Conversation:
    """The thread for a document, made on first use.

    Get-or-create rather than create: opening a document twice is one
    conversation, not two, and the panel would otherwise lose its history every
    time the user navigated away and back.
    """
    existing = db.scalar(
        select(Conversation)
        .where(
            Conversation.workspace_id == workspace_id,
            Conversation.document_id == document_id,
        )
        .order_by(Conversation.created_at.asc())
    )
    if existing is not None:
        return existing
    conversation = Conversation(
        workspace_id=workspace_id,
        created_by=user_id,
        title=title[:200],
        document_id=document_id,
    )
    db.add(conversation)
    db.flush()
    return conversation


def for_document_ids(db: Session, *, workspace_id: str, document_id: str) -> List[str]:
    return list(
        db.scalars(
            select(Conversation.id).where(
                Conversation.workspace_id == workspace_id,
                Conversation.document_id == document_id,
            )
        )
    )


def purge(db: Session, *, workspace_id: str, conversation_id: str) -> Optional[str]:
    """Delete a conversation and everything hanging off its runs.

    Returns the title, so a caller can audit what it removed, or None if there
    was nothing there. Does not commit: the caller decides what else belongs in
    the same transaction — for a document deletion, the document itself does.
    """
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.workspace_id == workspace_id,
        )
    )
    if conversation is None:
        return None
    title = conversation.title
    run_ids = list(
        db.scalars(
            select(Run.id).where(
                Run.conversation_id == conversation.id,
                Run.workspace_id == workspace_id,
            )
        )
    )
    if run_ids:
        db.execute(delete(ToolCall).where(ToolCall.run_id.in_(run_ids)))
        # The agent loop's own call rows. They were left behind before this was
        # shared, which was survivable only because `GET /api/documents-pending`
        # joins the run and the run was going too; an orphan row is still an
        # orphan row and a workspace should not accumulate them.
        db.execute(delete(AgentToolCall).where(AgentToolCall.run_id.in_(run_ids)))
        db.execute(delete(RunEvent).where(RunEvent.run_id.in_(run_ids)))
    db.execute(
        delete(Message).where(
            Message.conversation_id == conversation.id,
            Message.workspace_id == workspace_id,
        )
    )
    db.execute(
        delete(Run).where(
            Run.conversation_id == conversation.id,
            Run.workspace_id == workspace_id,
        )
    )
    db.delete(conversation)
    return title
