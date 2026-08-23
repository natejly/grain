"""The notifications store: one generic "a person should look at this" row.

Kept deliberately kind-agnostic. A mention writes `kind='mention'` today; a
metric monitor writes `kind='monitor_alert'` and a spend watcher
`kind='spend_anomaly'` tomorrow — all through this one `notify` helper, so the
Inbox contract (workspace-scoped, `status='open'` is the unbounded waiting
set, `target_user_id` '' means every member) is stated once rather than once
per feature.

Like every service here, nothing commits: the route owns the transaction, and
a notification rides in the same commit as the thing it announces — a comment
that fails to insert must not leave a mention pointing at nothing.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..clock import utcnow
from ..models import Notification


def notify(
    db: Session,
    *,
    workspace_id: str,
    kind: str,
    title: str,
    target_user_id: str = "",
    body: str = "",
    conversation_id: str = "",
    document_id: str = "",
    dashboard_id: str = "",
    comment_id: str = "",
    monitor_id: str = "",
    agent_id: str = "",
    created_by: str = "",
) -> Notification:
    """Add one open notification. Flushes so the caller can read its id."""
    notification = Notification(
        workspace_id=workspace_id,
        target_user_id=target_user_id,
        kind=kind,
        status="open",
        title=title,
        body=body,
        conversation_id=conversation_id,
        document_id=document_id,
        dashboard_id=dashboard_id,
        comment_id=comment_id,
        monitor_id=monitor_id,
        agent_id=agent_id,
        created_by=created_by,
    )
    db.add(notification)
    db.flush()
    return notification


def resolve(db: Session, *, notification: Notification, resolved_by: str) -> Notification:
    """Flip one notification out of the waiting set. Idempotent: resolving a
    resolved row changes nothing, so a double-click never rewrites who resolved
    it or when."""
    if notification.status == "resolved":
        return notification
    notification.status = "resolved"
    notification.resolved_at = utcnow()
    notification.resolved_by = resolved_by
    return notification
