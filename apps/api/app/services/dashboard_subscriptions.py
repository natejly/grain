"""The subscription ticker: mailing one member one dashboard, on a schedule.

A sibling of `services/crons.py`, folded into the same `POST /api/workflows/tick`
behind the same secret, with the same safety story and two deliberate
differences:

**The claim window is a day, not a minute.** A subscription is typically daily
("every morning at 9:00"), and the tick's usual one-minute `CATCHUP` would turn
an hour of ticker downtime at 9:00 into a silently skipped day of mail. So
`_firing_minute` scans back up to 24 hours for the most recent scheduled minute
not yet claimed — the "fire if not dispatched since the period start" shape the
daily-job convention asks for. The conditional UPDATE on `last_dispatched_at`
is still what makes it at-most-once; the window only decides how late a fire
may be, never how many there are.

**The work leaves the tick.** `dispatch_due` only claims; the dashboard query
and the SMTP conversation run in `send_subscription`, a background entrypoint
with its own session, enqueued by the tick — a slow mail host must not delay
monitor dispatch (the F5 QA note in tasks/todo.md says so in as many words).

Everything else is the house sweep contract: the dashboard and the recipient
are re-resolved under the subscription's own workspace at every fire, a target
that has since gone (purged dashboard, departed member, unanswerable query) is
a skip with a `dashboard.subscription_skipped` audit, and no failure ever
raises out of the shared ticker. A fire *reads and mails* — no run, no agent,
no policy question, because nothing here can act.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, List, Optional, Sequence, cast

from pydantic import ValidationError
from sqlalchemy import or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from ..clock import utcnow
from ..config import Settings, get_settings
from ..database import SessionLocal
from ..models import Dashboard, DashboardSubscription, Membership, User
from ..schemas import DashboardSpec
from . import mail_render
from .analytics import AnalyticsValidationError, execute_dataset_query
from .audit import record_audit
from .auth import email as email_service
from .workflows import schedule
from .workflows.validate import cron_matches

logger = logging.getLogger(__name__)

#: How far back a missed scheduled minute may still fire. A day, not the tick's
#: usual minute: "the 9:00 mail arrives at 9:37 because the ticker was down" is
#: the behaviour a daily subscription wants; "yesterday's AND today's mail
#: arrive together" is not, and the scan stopping at `last_dispatched_at` (plus
#: this cap) is what forbids it.
CATCHUP = timedelta(hours=24)

#: An email is a summary, not an export — the same posture as the public share
#: surface's row cap, sized for a mail client rather than a browser.
EMAIL_ROW_CAP = 50


def due(subscription: DashboardSubscription, *, moment: datetime) -> bool:
    """Is this subscription scheduled to fire in the minute `moment` falls in?"""
    if not subscription.enabled:
        return False
    local = schedule.local_moment(moment, subscription.schedule_timezone)
    if local is None:
        logger.warning(
            "dashboard subscription %s has an unknown timezone %r; not dispatching",
            subscription.id,
            subscription.schedule_timezone,
        )
        return False
    return cron_matches(subscription.schedule_cron, local)


def claim(
    db: Session, subscription: DashboardSubscription, *, minute: datetime
) -> bool:
    """Advance `last_dispatched_at` to `minute`, once. True means we won it.

    The whole of the at-most-once guarantee is this one conditional UPDATE —
    an exact mirror of `crons.claim`. The loser of a race gets `rowcount == 0`
    and moves on.
    """
    result = cast(
        "CursorResult[Any]",
        db.execute(
            update(DashboardSubscription)
            .where(
                DashboardSubscription.id == subscription.id,
                or_(
                    DashboardSubscription.last_dispatched_at.is_(None),
                    DashboardSubscription.last_dispatched_at < minute,
                ),
            )
            .values(last_dispatched_at=minute)
        ),
    )
    db.commit()
    return bool(result.rowcount == 1)


def dispatch_due(db: Session, *, moment: Optional[datetime] = None) -> List[str]:
    """Claim every enabled subscription due now; return their ids.

    Claiming only — the caller enqueues `send_subscription` for each id on a
    background task, so a slow dashboard query or mail host cannot make the
    shared tick time out.
    """
    now = schedule.floor_minute(moment or utcnow())
    candidates = list(
        db.scalars(
            select(DashboardSubscription).where(
                DashboardSubscription.enabled.is_(True),
                or_(
                    DashboardSubscription.last_dispatched_at.is_(None),
                    DashboardSubscription.last_dispatched_at < now,
                ),
            )
        )
    )
    claimed: List[str] = []
    for subscription in candidates:
        minute = _firing_minute(subscription, now)
        if minute is None:
            continue
        if not claim(db, subscription, minute=minute):
            continue
        claimed.append(subscription.id)
    db.commit()
    return claimed


def _firing_minute(
    subscription: DashboardSubscription, now: datetime
) -> Optional[datetime]:
    """The most recent minute this subscription is due for, or None.

    `crons._firing_minute` with the day-wide window: scanning newest-first and
    stopping at `last_dispatched_at` means at most ONE minute is ever returned
    — a ticker that was down past several scheduled minutes delivers the most
    recent one and deliberately drops the rest, because two days of downtime
    must not become two days of email in one go.
    """
    for offset in range(int(CATCHUP.total_seconds() // 60) + 1):
        candidate = now - timedelta(minutes=offset)
        if (
            subscription.last_dispatched_at is not None
            and candidate <= subscription.last_dispatched_at
        ):
            break
        if due(subscription, moment=candidate):
            return candidate
    return None


def send_subscription(subscription_id: str) -> None:
    """Background entrypoint: deliver one claimed subscription's mail.

    Owns its session, like `process_run`; re-loads by id and shrugs off a row
    deleted between claim and send. Never raises — delivery is best effort by
    design, and a failure here is a log line, not a broken tick.
    """
    db = SessionLocal()
    try:
        # A plain id lookup, not `db.get`: the id names a row the sweep just
        # claimed, and the select keeps this file out of the reviewed
        # primary-key-fetch allowlist for free.
        subscription = db.scalar(
            select(DashboardSubscription).where(
                DashboardSubscription.id == subscription_id
            )
        )
        if subscription is None or not subscription.enabled:
            return
        deliver(db, subscription)
    except Exception:  # noqa: BLE001 — background mail must never propagate
        logger.exception("dashboard subscription %s delivery failed", subscription_id)
        db.rollback()
    finally:
        db.close()


def deliver(
    db: Session,
    subscription: DashboardSubscription,
    *,
    settings: Optional[Settings] = None,
) -> bool:
    """Resolve, query live, render, send. True iff a mail went out.

    Every miss is a skip with a `dashboard.subscription_skipped` audit naming
    its reason — a purged dashboard, a departed member, a query that can no
    longer answer — because unattended mail that silently stops is a support
    ticket with no evidence. Commits its own work, like `crons.fire`: the
    caller owns no transaction across a send.
    """
    settings = settings or get_settings()
    dashboard = db.scalar(
        select(Dashboard).where(
            Dashboard.id == subscription.dashboard_id,
            # The subscription's OWN workspace, never a caller's: a foreign or
            # purged dashboard id resolves to nothing here and becomes a skip.
            Dashboard.workspace_id == subscription.workspace_id,
        )
    )
    if dashboard is None:
        return _skip(db, subscription, reason="dashboard gone")
    recipient = db.execute(
        select(User.email)
        .join(Membership, Membership.user_id == User.id)
        .where(
            Membership.workspace_id == subscription.workspace_id,
            Membership.user_id == subscription.recipient_user_id,
        )
    ).first()
    if recipient is None:
        # The membership row is the standing permission to receive this
        # workspace's data; the moment it goes, so does the mail.
        return _skip(db, subscription, reason="recipient is no longer a member")
    try:
        spec = DashboardSpec.model_validate(json.loads(dashboard.spec_json))
        result = execute_dataset_query(
            db,
            workspace_id=subscription.workspace_id,
            dataset_id=dashboard.dataset_id,
            query=spec.query,
        )
    except (AnalyticsValidationError, ValidationError, ValueError) as exc:
        return _skip(db, subscription, reason=str(exc) or "query failed")
    rows = [
        [row.get(column) for column in result.columns]
        for row in result.rows[:EMAIL_ROW_CAP]
    ]
    message = email_service.OutboundEmail(
        to=recipient.email,
        subject=f"Dashboard: {dashboard.name}",
        body=_text_body(dashboard.name, result.columns, rows, settings),
        html=(
            mail_render.render_table(dashboard.name, result.columns, rows)
            + mail_render.render_link_button(
                "Open in Grain", settings.primary_web_origin
            )
        ),
    )
    email_service.send_quietly(email_service.get_email_sender(settings), message)
    record_audit(
        db,
        workspace_id=subscription.workspace_id,
        actor_id=subscription.created_by,
        action="dashboard.subscription_sent",
        resource_type="dashboard_subscription",
        resource_id=subscription.id,
        detail={
            "dashboard_id": dashboard.id,
            "recipient_user_id": subscription.recipient_user_id,
            "rows": len(rows),
        },
    )
    db.commit()
    return True


def _text_body(
    title: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[object]],
    settings: Settings,
) -> str:
    """The complete plain-text alternative — every HTML mail must carry one.

    A pipe-separated table rather than a summary: a text-only client gets the
    same numbers the HTML client does, not an invitation to go look them up.
    """
    lines = [title, ""]
    if columns:
        lines.append(" | ".join(str(column) for column in columns))
    for row in rows:
        lines.append(
            " | ".join("" if value is None else str(value) for value in row)
        )
    if not rows:
        lines.append("(no rows)")
    lines.extend(["", f"Open in Grain: {settings.primary_web_origin}"])
    return "\n".join(lines)


def _skip(db: Session, subscription: DashboardSubscription, *, reason: str) -> bool:
    """Record that this fire delivered nothing, and move on."""
    logger.warning(
        "dashboard subscription %s skipped: %s", subscription.id, reason
    )
    record_audit(
        db,
        workspace_id=subscription.workspace_id,
        actor_id=subscription.created_by,
        action="dashboard.subscription_skipped",
        resource_type="dashboard_subscription",
        resource_id=subscription.id,
        detail={"reason": reason[:300]},
    )
    db.commit()
    return False
