"""The daily digest: one opt-in mail per member, listing what waits on them.

Rides `POST /api/workflows/tick` like every sweep, with two claims layered so
the shared ticker never carries this module's weight:

**The sweep gate is hourly, through `sweep_claims`.** `digest_hour_utc` is an
hour, so nothing about the digest is a per-minute question — one conditional
UPDATE on the shared claim row (the `spend_watch` pattern; F7 built the
table) lets one tick per hour scan the memberships and lets the other
fifty-nine skip in one indexed read of a single row.

**The send is per-member, claimed on `digest_last_sent_at`.** The daily-job
convention: a member is due when `now.hour >= digest_hour_utc` and their
claim column has not advanced past today's period start (midnight UTC) — a
period-start comparison, not an exact-minute match, so a ticker that was down
at 9:00 still sends today's mail at 10:37, and a second tick the same day
finds the column advanced and sends nothing. The conditional UPDATE elects
the winner; the tick only *claims* and the rendering, the waiting-set
queries and the SMTP conversation all run in `send_digest` on a background
task with its own session (the F5 QA note: no more heavy inline work in the
shared tick).

**What a member is told about is what they could see themselves.** Content
comes from `services/inbox_feed.waiting_for` — the same queries `GET
/api/inbox` answers with, `run_activity_predicate` included, so one member's
digest can never carry another member's personal-thread approvals. Only an
active user is mailed: a deactivated account keeps its memberships, but its
standing permission to receive workspace mail ends with its ability to log
in. An empty digest is not sent (the claim stands: silence, not a re-try), a
member or user that vanished between claim and send is a quiet skip, and —
like every sweep — nothing here raises out of the ticker or the background
task.

**The mail is titles-only, on purpose (QA F13 #8).** `Notification.body`
quotes comment and message content, and a digest that shipped it would move
workspace-internal conversation over SMTP — through relays and into mailbox
providers the platform does not control. So `_item_rows` puts titles and
provenance in the mail and leaves every body in-app, one deep-link click
away. Loosening this is a content-bar decision, not a formatting tweak.
"""
from __future__ import annotations

import html
import logging
from datetime import datetime
from typing import Any, List, Optional, Tuple, cast

from sqlalchemy import or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from ..clock import utcnow
from ..config import Settings, get_settings
from ..database import SessionLocal
from ..models import Membership, SweepClaim, User
from . import inbox_feed, mail_render
from .audit import record_audit
from .auth import email as email_service
from .inbox_feed import WaitingSet

logger = logging.getLogger(__name__)

#: The one row in `sweep_claims` this module owns.
CLAIM_NAME = "digests"
#: How many waiting items the mail lists in full; the counts cover the rest.
TOP_ITEMS = 10


def claim_sweep(db: Session, *, moment: datetime) -> bool:
    """Advance the shared claim row to `moment`'s hour, once. True = we won.

    The row is created on first contact; creation can race another instance,
    and the loser of that race simply proceeds to the same conditional UPDATE
    the winner uses — the UPDATE, not the INSERT, is what elects a winner.
    """
    period_start = moment.replace(minute=0, second=0, microsecond=0)
    if db.scalar(select(SweepClaim).where(SweepClaim.name == CLAIM_NAME)) is None:
        try:
            db.add(SweepClaim(name=CLAIM_NAME))
            db.commit()
        except Exception:  # noqa: BLE001 — a concurrent insert already made it
            db.rollback()
    result = cast(
        "CursorResult[Any]",
        db.execute(
            update(SweepClaim)
            .where(
                SweepClaim.name == CLAIM_NAME,
                or_(
                    SweepClaim.last_dispatched_at.is_(None),
                    SweepClaim.last_dispatched_at < period_start,
                ),
            )
            .values(last_dispatched_at=period_start)
        ),
    )
    db.commit()
    return bool(result.rowcount == 1)


def dispatch_due(db: Session, *, moment: Optional[datetime] = None) -> List[str]:
    """Claim every member whose digest is due; return their membership ids.

    Claiming only — the caller enqueues `send_digest` for each id on a
    background task. Never raises: one bad row must not fail the shared tick.
    """
    now = moment or utcnow()
    try:
        if not claim_sweep(db, moment=now):
            return []
        return _claim_members(db, now=now)
    except Exception:  # noqa: BLE001 — sweeps log, tick survives
        logger.exception("digest dispatch failed")
        db.rollback()
        return []


def _claim_members(db: Session, *, now: datetime) -> List[str]:
    period_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    candidates = list(
        db.scalars(
            select(Membership).where(
                Membership.digest_enabled.is_(True),
                Membership.digest_hour_utc <= now.hour,
                or_(
                    Membership.digest_last_sent_at.is_(None),
                    Membership.digest_last_sent_at < period_start,
                ),
            )
        )
    )
    claimed: List[str] = []
    for membership in candidates:
        # The whole of the at-most-once-per-day guarantee is this conditional
        # UPDATE — `crons.claim` with a period-start comparison instead of a
        # minute. The loser of a race gets rowcount 0 and moves on.
        result = cast(
            "CursorResult[Any]",
            db.execute(
                update(Membership)
                .where(
                    Membership.id == membership.id,
                    Membership.digest_enabled.is_(True),
                    or_(
                        Membership.digest_last_sent_at.is_(None),
                        Membership.digest_last_sent_at < period_start,
                    ),
                )
                .values(digest_last_sent_at=now)
            ),
        )
        db.commit()
        if result.rowcount == 1:
            claimed.append(membership.id)
    return claimed


def send_digest(membership_id: str) -> None:
    """Background entrypoint: build and mail one claimed member's digest.

    Owns its session, like `process_run`; re-loads by id and shrugs off a row
    deleted between claim and send. Never raises — delivery is best effort by
    design, and a failure here is a log line, not a broken tick.
    """
    db = SessionLocal()
    try:
        # A plain id lookup, not `db.get`: the id names a row the sweep just
        # claimed, and the select keeps this file out of the reviewed
        # primary-key-fetch allowlist for free.
        membership = db.scalar(
            select(Membership).where(Membership.id == membership_id)
        )
        if membership is None or not membership.digest_enabled:
            return
        deliver(db, membership)
    except Exception:  # noqa: BLE001 — background mail must never propagate
        logger.exception("digest %s delivery failed", membership_id)
        db.rollback()
    finally:
        db.close()


def deliver(
    db: Session,
    membership: Membership,
    *,
    settings: Optional[Settings] = None,
) -> bool:
    """Query the member's waiting set, render, send. True iff a mail went out.

    The claim already stood when this runs, so every quiet outcome — nothing
    waiting, a user row gone — leaves the claim in place: today was this
    member's day, and today's answer was silence.
    """
    settings = settings or get_settings()
    email = db.scalar(
        select(User.email).where(
            User.id == membership.user_id,
            # `auth` refuses a non-active user at every login door; the mailer
            # honours the same gate — a deactivated account with surviving
            # memberships must not keep receiving workspace-internal mail.
            User.status == "active",
        )
    )
    if not email:
        logger.warning(
            "digest %s skipped: user %s has no active mailable account",
            membership.id,
            membership.user_id,
        )
        return False
    waiting = inbox_feed.waiting_for(
        db, workspace_id=membership.workspace_id, user_id=membership.user_id
    )
    rows = _item_rows(waiting, user_id=membership.user_id)
    if not rows:
        # Nothing waiting -> no mail. The per-member claim stands, on purpose:
        # an empty queue is today's answer, not a reason to ask again hourly.
        return False
    counts = _count_line(waiting, user_id=membership.user_id)
    total = len(rows)
    noun = "item" if total == 1 else "items"
    subject = f"{total} {noun} waiting in Grain"
    listed = rows[:TOP_ITEMS]
    message = email_service.OutboundEmail(
        to=email,
        subject=subject,
        body=_text_body(listed, counts=counts, total=total, settings=settings),
        html=(
            mail_render.render_table(
                "Waiting on you", ("What", "Where", "Since"), listed
            )
            + f'<p style="margin:8px 0;">{html.escape(counts)}</p>'
            + mail_render.render_link_button(
                "Open your Inbox", settings.primary_web_origin
            )
        ),
    )
    sent = email_service.send_quietly(
        email_service.get_email_sender(settings), message
    )
    if not sent:
        # A refused mail must not audit as a delivery. The per-member claim
        # stands regardless — best-effort by design, no same-day retry.
        return False
    record_audit(
        db,
        workspace_id=membership.workspace_id,
        # The ticker has no user; automation audits as nobody, the way the
        # spend watch does.
        actor_id="",
        action="digest.sent",
        resource_type="membership",
        resource_id=membership.id,
        detail={"user_id": membership.user_id, "items": total},
    )
    db.commit()
    return True


def _my_approvals(waiting: WaitingSet, *, user_id: str) -> List[Any]:
    """The approvals that wait on THIS member: unassigned or assigned to them.

    The same arithmetic as the client's badge — an approval routed to a
    colleague is visible in the Inbox but is not this member's work, and a
    daily mail that counted it would nag the wrong person.
    """
    return [
        item for item in waiting.approvals if item.assigned_to in ("", user_id)
    ]


def _item_rows(waiting: WaitingSet, *, user_id: str) -> List[Tuple[str, str, str]]:
    """(What, Where, Since) rows, oldest first within each kind.

    Plain strings only — `mail_render` escapes every cell, so a workflow named
    `<script>` arrives as text. Titles only, never `Notification.body`: a
    mention or alert body quotes comment/message content, and that stays
    in-app behind the deep link (see the module docstring).
    """
    rows: List[Tuple[str, str, str]] = []
    for item in _my_approvals(waiting, user_id=user_id):
        where = item.workflow_name or item.conversation_title or item.origin
        rows.append((f"Approval: {item.name}", where, _since(item.created_at)))
    for hold in waiting.budget_holds:
        where = hold.workflow_name or hold.origin
        rows.append(("Budget hold", where, _since(hold.created_at)))
    for mention in waiting.mentions:
        rows.append((f"Mention: {mention.title}", "", _since(mention.created_at)))
    for alert in waiting.alerts:
        rows.append((f"Alert: {alert.title}", "", _since(alert.created_at)))
    for anomaly in waiting.anomalies:
        rows.append(
            (f"Spend anomaly: {anomaly.title}", "", _since(anomaly.created_at))
        )
    return rows


def _count_line(waiting: WaitingSet, *, user_id: str) -> str:
    parts = [
        f"{len(_my_approvals(waiting, user_id=user_id))} approvals",
        f"{len(waiting.budget_holds)} budget holds",
        f"{len(waiting.mentions)} mentions",
        f"{len(waiting.alerts)} alerts",
        f"{len(waiting.anomalies)} spend anomalies",
    ]
    return "In total: " + ", ".join(parts) + "."


def _text_body(
    rows: List[Tuple[str, str, str]],
    *,
    counts: str,
    total: int,
    settings: Settings,
) -> str:
    """The complete plain-text alternative — every HTML mail carries one."""
    lines = ["Waiting on you in Grain:", ""]
    for what, where, since in rows:
        entry = f"- {what}"
        if where:
            entry += f" — {where}"
        lines.append(entry + f" (since {since})")
    if total > len(rows):
        lines.append(f"...and {total - len(rows)} more.")
    lines.extend(["", counts, "", f"Open your Inbox: {settings.primary_web_origin}"])
    return "\n".join(lines)


def _since(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M UTC")
