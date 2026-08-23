"""Comments on things, and the notifications @mentions produce.

A comment hangs on the established polymorphic pair (`subject_kind` +
`subject_id`) — a thread, a document, or a dashboard — and the subject is
resolved FIRST on every route, so a foreign or missing subject is uniformly a
404 before anything else can answer. Conversation subjects additionally pass
`conversations.resolve_visible`: a comment thread on a personal chat is
exactly as private as the chat.

Mentions are validated server-side against actual workspace members, and a
user id that is not a member of the caller's workspace is silently dropped —
never echoed, never erred on — so this route cannot be used to confirm foreign
user ids. A mention on a private conversation only notifies members who can
see that conversation; the others are dropped the same silent way.

The notification resolve endpoint lives here too (the store's first writer is
its natural host): it flips `status`, after which the Inbox simply stops
listing the row.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..clock import utcnow
from ..database import get_db
from ..models import Comment, Dashboard, Document, Membership, Notification, User
from ..schemas import ApiModel
from ..services import conversations, notifications
from ..services.audit import record_audit
from .dependencies import idempotency_key
from .idempotency import find_replay, record_key, replayed_resource_gone

router = APIRouter(prefix="/api", tags=["comments"])


class MemberOut(ApiModel):
    """One workspace colleague, as the @-picker needs them: an id to mention
    and a name to show. Deliberately not the admin surface — no email, no
    join date — because every member may see who they can mention."""

    user_id: str
    name: str
    role: str


class CommentCreate(BaseModel):
    subject_kind: Literal["conversation", "document", "dashboard"]
    subject_id: str = Field(min_length=1, max_length=64)
    body: str = Field(min_length=1, max_length=10_000)
    #: User ids this comment @-mentions. Anything that is not a member of the
    #: caller's workspace is dropped without comment.
    mentions: List[str] = Field(default_factory=list, max_length=50)


class CommentOut(ApiModel):
    id: str
    subject_kind: str
    subject_id: str
    body: str
    #: The mentions that were actually kept — valid members only.
    mentions: List[str]
    created_by: str
    created_at: datetime
    updated_at: datetime


class NotificationOut(ApiModel):
    id: str
    kind: str
    status: str
    target_user_id: str
    title: str
    body: str
    conversation_id: str
    document_id: str
    dashboard_id: str
    comment_id: str
    created_by: str
    created_at: datetime
    resolved_at: Optional[datetime]
    resolved_by: str


def _out(comment: Comment) -> CommentOut:
    try:
        mentions = json.loads(comment.mentions_json or "[]")
    except ValueError:
        mentions = []
    return CommentOut(
        id=comment.id,
        subject_kind=comment.subject_kind,
        subject_id=comment.subject_id,
        body=comment.body,
        mentions=[str(item) for item in mentions],
        created_by=comment.created_by,
        created_at=comment.created_at,
        updated_at=comment.updated_at,
    )


def _notification_out(notification: Notification) -> NotificationOut:
    return NotificationOut(
        id=notification.id,
        kind=notification.kind,
        status=notification.status,
        target_user_id=notification.target_user_id,
        title=notification.title,
        body=notification.body,
        conversation_id=notification.conversation_id,
        document_id=notification.document_id,
        dashboard_id=notification.dashboard_id,
        comment_id=notification.comment_id,
        created_by=notification.created_by,
        created_at=notification.created_at,
        resolved_at=notification.resolved_at,
        resolved_by=notification.resolved_by,
    )


def _resolve_subject(
    db: Session, *, actor: Actor, subject_kind: str, subject_id: str
) -> None:
    """404 unless the subject exists in the caller's workspace and, for a
    conversation, is visible to the caller. Resolved before anything else so a
    foreign id is indistinguishable from a missing one."""
    if subject_kind == "conversation":
        visible = conversations.resolve_visible(
            db,
            workspace_id=actor.workspace_id,
            user_id=actor.user_id,
            conversation_id=subject_id,
        )
        if visible is None:
            raise HTTPException(status_code=404, detail="Subject not found")
        return
    if subject_kind == "document":
        row = db.scalar(
            select(Document.id).where(
                Document.id == subject_id,
                Document.workspace_id == actor.workspace_id,
            )
        )
    elif subject_kind == "dashboard":
        row = db.scalar(
            select(Dashboard.id).where(
                Dashboard.id == subject_id,
                Dashboard.workspace_id == actor.workspace_id,
            )
        )
    else:
        raise HTTPException(status_code=404, detail="Subject not found")
    if row is None:
        raise HTTPException(status_code=404, detail="Subject not found")


def _kept_mentions(
    db: Session, *, actor: Actor, subject_kind: str, subject_id: str, mentions: List[str]
) -> List[str]:
    """The mention ids that survive validation, in request order.

    Only actual members of the caller's workspace are kept — a foreign user id
    is dropped silently, never confirmed. On a conversation subject the member
    must additionally be able to SEE the conversation (`resolve_visible` as
    them), or a mention on a private thread would hand a non-viewer a deep
    link into it.
    """
    if not mentions:
        return []
    candidates = [str(item) for item in dict.fromkeys(mentions)]
    member_ids = set(
        db.scalars(
            select(Membership.user_id).where(
                Membership.workspace_id == actor.workspace_id,
                Membership.user_id.in_(candidates),
            )
        )
    )
    kept = [user_id for user_id in candidates if user_id in member_ids]
    if subject_kind == "conversation":
        kept = [
            user_id
            for user_id in kept
            if conversations.resolve_visible(
                db,
                workspace_id=actor.workspace_id,
                user_id=user_id,
                conversation_id=subject_id,
            )
            is not None
        ]
    return kept


@router.get("/members", response_model=List[MemberOut])
def list_members(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[MemberOut]:
    """Who can be @-mentioned here: every member of the caller's workspace.

    The admin member list (`/api/admin/members`) is owner-only because it
    carries emails and standing; this one exists so a plain member's composer
    can complete a colleague's name, and carries nothing an invite email did
    not already reveal to the whole workspace.
    """
    rows = db.execute(
        select(Membership.user_id, User.name, Membership.role)
        .join(User, User.id == Membership.user_id)
        .where(Membership.workspace_id == actor.workspace_id)
        .order_by(User.name.asc())
    ).all()
    return [
        MemberOut(user_id=user_id, name=name, role=role)
        for user_id, name, role in rows
    ]


@router.post("/comments", response_model=CommentOut, status_code=201)
def create_comment(
    payload: CommentCreate,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> CommentOut:
    replay = find_replay(
        db, workspace_id=actor.workspace_id, operation="comment.create", key=key
    )
    if replay:
        comment = db.scalar(
            select(Comment).where(
                Comment.id == replay.resource_id,
                Comment.workspace_id == actor.workspace_id,
            )
        )
        if comment is None:
            raise replayed_resource_gone()
        return _out(comment)

    _resolve_subject(
        db,
        actor=actor,
        subject_kind=payload.subject_kind,
        subject_id=payload.subject_id,
    )
    kept = _kept_mentions(
        db,
        actor=actor,
        subject_kind=payload.subject_kind,
        subject_id=payload.subject_id,
        mentions=payload.mentions,
    )
    comment = Comment(
        workspace_id=actor.workspace_id,
        subject_kind=payload.subject_kind,
        subject_id=payload.subject_id,
        body=payload.body,
        mentions_json=json.dumps(kept),
        created_by=actor.user_id,
    )
    db.add(comment)
    db.flush()

    actor_name = (
        db.scalar(select(User.name).where(User.id == actor.user_id)) or "Someone"
    )
    for member_id in kept:
        notifications.notify(
            db,
            workspace_id=actor.workspace_id,
            kind="mention",
            target_user_id=member_id,
            title=f"{actor_name} mentioned you",
            body=comment.body,
            conversation_id=(
                payload.subject_id if payload.subject_kind == "conversation" else ""
            ),
            document_id=(
                payload.subject_id if payload.subject_kind == "document" else ""
            ),
            dashboard_id=(
                payload.subject_id if payload.subject_kind == "dashboard" else ""
            ),
            comment_id=comment.id,
            created_by=actor.user_id,
        )

    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="comment.create",
        key=key,
        resource_id=comment.id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="comment.created",
        resource_type="comment",
        resource_id=comment.id,
        detail={
            "subject_kind": payload.subject_kind,
            "subject_id": payload.subject_id,
            "mentions": len(kept),
        },
    )
    db.commit()
    db.refresh(comment)
    return _out(comment)


@router.get("/comments", response_model=List[CommentOut])
def list_comments(
    subject_kind: str = Query(min_length=1, max_length=16),
    subject_id: str = Query(min_length=1, max_length=64),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[CommentOut]:
    _resolve_subject(
        db, actor=actor, subject_kind=subject_kind, subject_id=subject_id
    )
    rows = db.scalars(
        select(Comment)
        .where(
            Comment.workspace_id == actor.workspace_id,
            Comment.subject_kind == subject_kind,
            Comment.subject_id == subject_id,
            Comment.deleted_at.is_(None),
        )
        .order_by(Comment.created_at.asc())
    ).all()
    return [_out(comment) for comment in rows]


@router.delete("/comments/{comment_id}", status_code=204)
def delete_comment(
    comment_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> None:
    """Soft-delete. Resolved under the workspace first so a foreign comment is
    a 404, then through the same subject-visibility gate as create/list — a
    comment on a conversation the caller cannot see answers 404 exactly like a
    missing one, even for the workspace owner. Only then does the author/owner
    gate answer 403. Naturally idempotent — deleting a deleted comment changes
    nothing — so no key."""
    comment = db.scalar(
        select(Comment).where(
            Comment.id == comment_id, Comment.workspace_id == actor.workspace_id
        )
    )
    if comment is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    _resolve_subject(
        db,
        actor=actor,
        subject_kind=comment.subject_kind,
        subject_id=comment.subject_id,
    )
    if comment.created_by != actor.user_id and actor.role != "owner":
        raise HTTPException(
            status_code=403, detail="Only the author or a workspace owner may delete"
        )
    if comment.deleted_at is None:
        comment.deleted_at = utcnow()
        # The mentions this comment produced snapshotted its body; the delete
        # must take those snapshots with it, or the text stays readable in
        # every mentioned inbox forever.
        cleared = notifications.resolve_for_comment(
            db,
            workspace_id=actor.workspace_id,
            comment_id=comment.id,
            resolved_by=actor.user_id,
        )
        record_audit(
            db,
            workspace_id=actor.workspace_id,
            actor_id=actor.user_id,
            action="comment.deleted",
            resource_type="comment",
            resource_id=comment.id,
            detail={
                "subject_kind": comment.subject_kind,
                "subject_id": comment.subject_id,
                "notifications_resolved": len(cleared),
            },
        )
        db.commit()


@router.post(
    "/notifications/{notification_id}/resolve", response_model=NotificationOut
)
def resolve_notification(
    notification_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> NotificationOut:
    """Flip one notification out of the waiting set.

    A row targeted at another member is answered exactly like a missing one:
    someone else's mention is not this caller's to resolve, and a 403 would
    confirm it exists.

    A ''-targeted (broadcast) row is any member's to resolve, and resolving it
    clears it workspace-wide — deliberately. Broadcast kinds (monitor alerts,
    spend anomalies) announce workspace facts, and acknowledgement is
    PagerDuty-style: the first member to ack has acked for the room. There is
    no per-member read state by design; who acted is recorded on the row
    (`resolved_by`) and audited below in the same transaction.
    """
    notification = db.scalar(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.workspace_id == actor.workspace_id,
        )
    )
    if notification is None or notification.target_user_id not in (
        "",
        actor.user_id,
    ):
        raise HTTPException(status_code=404, detail="Notification not found")
    if notification.status != "resolved":
        notifications.resolve(db, notification=notification, resolved_by=actor.user_id)
        record_audit(
            db,
            workspace_id=actor.workspace_id,
            actor_id=actor.user_id,
            action="notification.resolved",
            resource_type="notification",
            resource_id=notification.id,
            detail={"kind": notification.kind},
        )
        db.commit()
        db.refresh(notification)
    return _notification_out(notification)
