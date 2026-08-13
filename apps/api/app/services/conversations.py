"""Conversations, and the things that own one.

A thread is normally its own thing: the user makes it, names it, deletes it. A
thread opened from the chat panel beside a document, a project or a dashboard is
not — it exists because its subject does, it is handed that subject's content on
every turn, and when the subject goes it has nothing left to be about. That
asymmetry is why the get-or-create and the cascade live here rather than inline
in a route: four routers act on it, and a cascade implemented four times is a
cascade that will disagree with itself.

The subject is polymorphic (`subject_kind` + `subject_id`) for the same reason
this module exists at all: every rule below is the SAME rule for all three
kinds, and three columns would be three places for each rule to forget one.
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import (
    AgentToolCall,
    Conversation,
    Message,
    Run,
    RunEvent,
    ToolCall,
    WorkflowRun,
)


def resolve_visible(
    db: Session, *, workspace_id: str, user_id: str, conversation_id: str
) -> Optional[Conversation]:
    """The one within-workspace visibility rule, in one place.

    A conversation is visible when, within the caller's workspace, it is the
    caller's own, OR it is shared, OR it is a subject thread (reached through the
    workspace-scoped document, project or dashboard, not the rail). Returns None
    so a foreign workspace_id or another member's personal thread becomes a 404
    at the call site, never a leak.

    The `workspace_id` filter is ALWAYS applied and is never removed: sharing
    only relaxes the *within-workspace* `created_by` filter from "creator only"
    to "creator OR any member". It can never expose a thread cross-workspace.

    The `subject_id != ""` clause is load-bearing: a subject thread is opened by
    any member beside a workspace-shared subject, so gating it on personal/shared
    would break all three side panels. (Existing document rows are also
    backfilled to shared, so this is belt-and-suspenders.)
    """
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.workspace_id == workspace_id,  # NEVER removed
        )
    )
    if conversation is None:
        return None
    if (
        conversation.created_by == user_id
        or conversation.shared
        or conversation.subject_id != ""
    ):
        return conversation
    return None


def run_activity_visible(
    db: Session, *, actor_workspace_id: str, actor_user_id: str, run: Run
) -> bool:
    """Whether a run's activity — its events, approvals, cancel, pending edits —
    is visible to this actor *within their workspace*.

    The sibling of `resolve_visible` for the run/tool-call surfaces. A run's
    conversation-scoped chat activity leaks to another member exactly as a
    personal thread's messages would, so the same within-workspace gate has to
    guard it. It is visible when EITHER:

    (a) the run is workspace-level AUTOMATION — workflow-backed (a WorkflowRun
        names it), a cron run (`cron_id != ""`), or a run with no chat
        conversation at all (`conversation_id == ""`). These are the Activity
        queue any member reviews, and they stay workspace-visible; OR
    (b) the run's conversation is visible to the actor via `resolve_visible`
        (own, shared, or a document thread).

    The caller has already loaded `run` under `workspace_id == actor_workspace_id`,
    so this only ever ADDS a within-workspace gate; it never relaxes the
    cross-workspace filter, which stays on every query.
    """
    if run.cron_id != "" or run.conversation_id == "":
        return True
    workflow_backed = db.scalar(
        select(WorkflowRun.id).where(WorkflowRun.run_id == run.id)
    )
    if workflow_backed is not None:
        return True
    return (
        resolve_visible(
            db,
            workspace_id=actor_workspace_id,
            user_id=actor_user_id,
            conversation_id=run.conversation_id,
        )
        is not None
    )


def run_activity_predicate(*, actor_user_id: str):  # type: ignore[no-untyped-def]
    """The SQL half of `run_activity_visible`, for the LIST surfaces.

    Keeps a listing one query: apply it with a LEFT JOIN of `Conversation` onto
    `Run.conversation_id`, alongside the `workspace_id` filter that is never
    removed. A row is kept when the run is automation (cron, workflow-backed, or
    has no chat conversation — the LEFT JOIN yields NULL) OR its conversation is
    visible to the actor (shared, own, or a subject thread), mirroring
    `run_activity_visible` clause for clause.
    """
    return (
        (Run.cron_id != "")
        | Run.id.in_(select(WorkflowRun.run_id).where(WorkflowRun.run_id.isnot(None)))
        | (Conversation.id.is_(None))
        | (Conversation.shared.is_(True))
        | (Conversation.created_by == actor_user_id)
        | (Conversation.subject_id != "")
    )


def for_subject(
    db: Session,
    *,
    workspace_id: str,
    subject_kind: str,
    subject_id: str,
    user_id: str,
    title: str,
) -> Conversation:
    """The thread for one subject, made on first use.

    Get-or-create rather than create: opening a document twice is one
    conversation, not two, and the panel would otherwise lose its history every
    time the user navigated away and back. The subject id *is* the idempotency
    key, which is why no route that reaches this needs an `Idempotency-Key`.
    """
    existing = db.scalar(
        select(Conversation)
        .where(
            Conversation.workspace_id == workspace_id,
            Conversation.subject_kind == subject_kind,
            Conversation.subject_id == subject_id,
        )
        .order_by(Conversation.created_at.asc())
    )
    if existing is not None:
        return existing
    conversation = Conversation(
        workspace_id=workspace_id,
        created_by=user_id,
        title=title[:200],
        subject_kind=subject_kind,
        subject_id=subject_id,
    )
    db.add(conversation)
    db.flush()
    return conversation


def for_subject_ids(
    db: Session, *, workspace_id: str, subject_kind: str, subject_id: str
) -> List[str]:
    """Every thread about this subject — what a deletion has to take with it."""
    return list(
        db.scalars(
            select(Conversation.id).where(
                Conversation.workspace_id == workspace_id,
                Conversation.subject_kind == subject_kind,
                Conversation.subject_id == subject_id,
            )
        )
    )


def purge_for_subject(
    db: Session, *, workspace_id: str, subject_kind: str, subject_id: str
) -> None:
    """Delete every thread about a subject that is going away.

    Does not commit: the caller decides what else belongs in the same
    transaction — for a project deletion, the project itself does.
    """
    for conversation_id in for_subject_ids(
        db,
        workspace_id=workspace_id,
        subject_kind=subject_kind,
        subject_id=subject_id,
    ):
        purge(db, workspace_id=workspace_id, conversation_id=conversation_id)


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
