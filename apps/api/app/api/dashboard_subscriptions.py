"""REST surface over dashboard subscriptions (scheduled snapshot mail).

Three workspace-scoped routes on the crons router's posture: the dashboard is
resolved under the caller's workspace FIRST so a foreign id uniformly 404s,
the schedule is validated exactly as a cron's is (422 while a person is still
holding the form, not silently inside a tick that never fires), and every
mutation audits.

Who may do what is deliberately small. A member subscribes *themselves*;
subscribing somebody else is an owner's move — signing a colleague up for
recurring mail is routing their attention, the `require_owner` class of act.
The recipient must be a member of this workspace: the mail carries workspace
data, and membership is the standing permission to see it (a non-member id
404s, indistinguishable from a user that does not exist). The list shows a
member their own subscriptions — created by them or addressed to them — and an
owner all of them; delete follows the same visibility, so a row you cannot
list is a row you cannot delete (404, not 403 — nothing is confirmed).

Dispatch is not here: the shared `POST /api/workflows/tick` claims due
subscriptions through `services/dashboard_subscriptions.py` and mails them on
a background task. One external cron, one secret, one URL.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..database import get_db
from ..models import Dashboard, DashboardSubscription, Membership
from ..schemas import ApiModel
from ..services.audit import record_audit
from ..services.workflows.validate import cron_error
from .dependencies import idempotency_key
from .idempotency import find_replay, record_key, replayed_resource_gone

router = APIRouter(prefix="/api/dashboard-subscriptions", tags=["dashboard-subscriptions"])


class DashboardSubscriptionOut(ApiModel):
    id: str
    dashboard_id: str
    #: Joined at read time and '' for a dashboard since purged — the list must
    #: say what a row mails without a second round trip, and must not invent a
    #: name for something that no longer exists.
    dashboard_name: str
    recipient_user_id: str
    schedule_cron: str
    schedule_timezone: str
    enabled: bool
    last_dispatched_at: Optional[datetime]
    created_by: str
    created_at: datetime


class DashboardSubscriptionCreateRequest(BaseModel):
    dashboard_id: str = Field(min_length=1, max_length=36)
    schedule_cron: str = Field(min_length=1, max_length=120)
    schedule_timezone: str = Field(default="UTC", max_length=64)
    #: '' means "me" — the common case needs no id at all.
    recipient_user_id: str = Field(default="", max_length=36)


def _out(
    subscription: DashboardSubscription, dashboard_names: Dict[str, str]
) -> DashboardSubscriptionOut:
    return DashboardSubscriptionOut(
        id=subscription.id,
        dashboard_id=subscription.dashboard_id,
        dashboard_name=dashboard_names.get(subscription.dashboard_id, ""),
        recipient_user_id=subscription.recipient_user_id,
        schedule_cron=subscription.schedule_cron,
        schedule_timezone=subscription.schedule_timezone,
        enabled=subscription.enabled,
        last_dispatched_at=subscription.last_dispatched_at,
        created_by=subscription.created_by,
        created_at=subscription.created_at,
    )


def _dashboard_names(db: Session, workspace_id: str) -> Dict[str, str]:
    rows = db.execute(
        select(Dashboard.id, Dashboard.name).where(
            Dashboard.workspace_id == workspace_id
        )
    ).all()
    return {row.id: row.name for row in rows}


def _validate_schedule(schedule_cron: str, timezone: str) -> None:
    """422 a bad cron or IANA zone at the boundary — `crons._validate_schedule`."""
    error = cron_error(schedule_cron)
    if error is not None:
        raise HTTPException(status_code=422, detail=error)
    try:
        ZoneInfo(timezone or "UTC")
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise HTTPException(
            status_code=422, detail=f"unknown timezone “{timezone}”"
        ) from exc


def _visible(subscription: DashboardSubscription, actor: Actor) -> bool:
    """An owner sees every subscription; a member their own — created by them
    or addressed to them. One predicate for the list and the delete, so a row
    you cannot list is exactly a row you cannot delete."""
    if actor.role == "owner":
        return True
    return actor.user_id in (subscription.created_by, subscription.recipient_user_id)


@router.post("", response_model=DashboardSubscriptionOut, status_code=201)
def create_dashboard_subscription(
    payload: DashboardSubscriptionCreateRequest,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> DashboardSubscriptionOut:
    replay = find_replay(
        db,
        workspace_id=actor.workspace_id,
        operation="dashboard_subscription.create",
        key=key,
    )
    if replay:
        subscription = db.scalar(
            select(DashboardSubscription).where(
                DashboardSubscription.id == replay.resource_id,
                DashboardSubscription.workspace_id == actor.workspace_id,
            )
        )
        if subscription is None:
            raise replayed_resource_gone()
        return _out(subscription, _dashboard_names(db, actor.workspace_id))
    # The dashboard first, so a foreign id 404s before any validation detail
    # (a bad cron's 422, the owner rule's 403) could confirm it exists.
    dashboard = db.scalar(
        select(Dashboard.id).where(
            Dashboard.id == payload.dashboard_id,
            Dashboard.workspace_id == actor.workspace_id,
        )
    )
    if dashboard is None:
        raise HTTPException(status_code=404, detail="Dashboard not found")
    _validate_schedule(payload.schedule_cron, payload.schedule_timezone)
    recipient_id = payload.recipient_user_id or actor.user_id
    if recipient_id != actor.user_id:
        member = db.scalar(
            select(Membership.id).where(
                Membership.workspace_id == actor.workspace_id,
                Membership.user_id == recipient_id,
            )
        )
        if member is None:
            # 404, indistinguishable from a user that does not exist — the
            # refusal must not confirm a foreign workspace's user id.
            raise HTTPException(status_code=404, detail="Member not found")
        if actor.role != "owner":
            raise HTTPException(
                status_code=403,
                detail="Only an owner can subscribe another member",
            )
    subscription = DashboardSubscription(
        workspace_id=actor.workspace_id,
        dashboard_id=payload.dashboard_id,
        recipient_user_id=recipient_id,
        schedule_cron=payload.schedule_cron,
        schedule_timezone=payload.schedule_timezone or "UTC",
        created_by=actor.user_id,
    )
    db.add(subscription)
    db.flush()
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="dashboard_subscription.create",
        key=key,
        resource_id=subscription.id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="dashboard.subscription_created",
        resource_type="dashboard_subscription",
        resource_id=subscription.id,
        detail={
            "dashboard_id": subscription.dashboard_id,
            "recipient_user_id": subscription.recipient_user_id,
            "schedule_cron": subscription.schedule_cron,
        },
    )
    db.commit()
    return _out(subscription, _dashboard_names(db, actor.workspace_id))


@router.get("", response_model=List[DashboardSubscriptionOut])
def list_dashboard_subscriptions(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[DashboardSubscriptionOut]:
    rows = list(
        db.scalars(
            select(DashboardSubscription)
            .where(DashboardSubscription.workspace_id == actor.workspace_id)
            .order_by(
                DashboardSubscription.created_at.desc(), DashboardSubscription.id
            )
        )
    )
    names = _dashboard_names(db, actor.workspace_id)
    return [_out(row, names) for row in rows if _visible(row, actor)]


@router.delete("/{subscription_id}", status_code=204)
def delete_dashboard_subscription(
    subscription_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> None:
    subscription = db.scalar(
        select(DashboardSubscription).where(
            DashboardSubscription.id == subscription_id,
            DashboardSubscription.workspace_id == actor.workspace_id,
        )
    )
    # One 404 for "not there", "not yours to see", and "another tenant's":
    # a member must not learn that a colleague's private morning mail exists
    # by probing ids, any more than a foreign tenant may.
    if subscription is None or not _visible(subscription, actor):
        raise HTTPException(status_code=404, detail="Subscription not found")
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="dashboard.subscription_deleted",
        resource_type="dashboard_subscription",
        resource_id=subscription.id,
        detail={
            "dashboard_id": subscription.dashboard_id,
            "recipient_user_id": subscription.recipient_user_id,
        },
    )
    db.delete(subscription)
    db.commit()
