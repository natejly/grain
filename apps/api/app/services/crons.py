"""The cron ticker: turning a stored personal cron into unattended work.

A sibling of `services/workflows/schedule.py`, folded into the *same*
`POST /api/workflows/tick` and sharing its one secret. Everything that makes the
workflow ticker safe to hand an unauthenticated caller holding a shared secret
applies here unchanged:

**It is a claim, not a trigger.** `crons.last_dispatched_at` is advanced by a
conditional UPDATE (`claim`), so a tick replayed, retried, or delivered to three
instances at once produces one fire per cron per minute.

**It cannot reach into the past.** The moment is the server's clock, and the
`CATCHUP` window fires only the just-missed minute — a ticker that catches up on
a day of downtime would send a day of email.

**It grants nothing.** A `kind="task"` fire creates an ordinary `Run` marked
with `cron_id`, and `policy_scope_for_run` reads that marker to run the turn at
`workflow` policy scope. The first write-capable tool parks and waits for a
human, exactly as a scheduled workflow does. The ticker starts work; it never
authorises it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, List, Optional, cast

from sqlalchemy import or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from ..clock import utcnow
from ..models import Agent, Conversation, Cron, Message, Run, new_id
from . import conversations
from .audit import record_audit
from .events import append_event
from .workflows import schedule
from .workflows.validate import cron_matches

logger = logging.getLogger(__name__)

#: One minute of grace covers a slow request and nothing else — the same window
#: the workflow ticker uses, and for the same reason.
CATCHUP = schedule.CATCHUP


def due(cron: Cron, *, moment: datetime) -> bool:
    """Is this cron scheduled to fire in the minute `moment` falls in?"""
    if not cron.enabled:
        return False
    local = schedule.local_moment(moment, cron.schedule_timezone)
    if local is None:
        logger.warning(
            "cron %s has an unknown timezone %r; not dispatching",
            cron.id,
            cron.schedule_timezone,
        )
        return False
    return cron_matches(cron.schedule_cron, local)


def claim(db: Session, cron: Cron, *, minute: datetime) -> bool:
    """Advance `last_dispatched_at` to `minute`, once. True means we won it.

    The whole of the at-most-once guarantee is this one conditional UPDATE — an
    exact mirror of `schedule.claim`. The loser of a race gets `rowcount == 0`
    and moves on.
    """
    result = cast(
        "CursorResult[Any]",
        db.execute(
            update(Cron)
            .where(
                Cron.id == cron.id,
                or_(
                    Cron.last_dispatched_at.is_(None),
                    Cron.last_dispatched_at < minute,
                ),
            )
            .values(last_dispatched_at=minute)
        ),
    )
    db.commit()
    return bool(result.rowcount == 1)


def dispatch_due(db: Session, *, moment: Optional[datetime] = None) -> List[str]:
    """Claim and enqueue every enabled cron due now.

    Returns the ids of the task runs it created — the caller starts each on the
    same background task a chat turn uses, so one slow turn cannot make the tick
    time out. A `kind="message"` fire posts synchronously and returns nothing to
    run.
    """
    now = schedule.floor_minute(moment or utcnow())
    candidates = list(
        db.scalars(
            select(Cron).where(
                Cron.enabled.is_(True),
                or_(
                    Cron.last_dispatched_at.is_(None),
                    Cron.last_dispatched_at < now,
                ),
            )
        )
    )
    started: List[str] = []
    for cron in candidates:
        minute = _firing_minute(cron, now)
        if minute is None:
            continue
        if not claim(db, cron, minute=minute):
            continue
        run_id = fire(db, cron)
        if run_id:
            started.append(run_id)
    db.commit()
    return started


def _firing_minute(cron: Cron, now: datetime) -> Optional[datetime]:
    """The minute this cron is due for, or None.

    A direct copy of `schedule._firing_minute`: `CATCHUP` covers the previous
    minute so a tick delayed past the boundary still fires it, but anything older
    is deliberately dropped — a scheduler that catches up on a day of downtime
    sends a day of email.
    """
    for offset in range(int(CATCHUP.total_seconds() // 60) + 1):
        candidate = now - timedelta(minutes=offset)
        if cron.last_dispatched_at is not None and candidate <= cron.last_dispatched_at:
            break
        if due(cron, moment=candidate):
            return candidate
    return None


def fire(db: Session, cron: Cron) -> Optional[str]:
    """Do the cron's work once. Returns a run id for a task, None for a message.

    Called both by `dispatch_due` (after the atomic claim, which owns the minute)
    and by run-now (which is claim-free — a person is asking for it now). The two
    kinds diverge here:
    a message posts into a conversation synchronously; a task creates a queued
    `Run` the caller then enqueues on the chat background path.
    """
    if cron.kind == "message":
        _post_message(db, cron)
        return None
    return _start_task_run(db, cron)


def _post_message(db: Session, cron: Cron) -> None:
    """Post the cron's body into its target conversation, created-or-named."""
    conv: Optional[Conversation] = None
    if cron.target_conversation_id:
        conv = db.scalar(
            select(Conversation).where(
                Conversation.id == cron.target_conversation_id,
                Conversation.workspace_id == cron.workspace_id,
            )
        )
    if conv is None:
        conv = Conversation(
            workspace_id=cron.workspace_id,
            created_by=cron.created_by,
            title=cron.name[:200] or "Automation",
            # The cron's owner is the member whose preference this reads: an
            # automation nobody is watching still belongs to whoever scheduled
            # it. Safe mode parks its writes for that person to answer, which
            # is slow but is what they asked for; off, it just runs.
            approval_mode=conversations.default_approval_mode(
                db, workspace_id=cron.workspace_id, user_id=cron.created_by
            ),
        )
        db.add(conv)
        db.flush()
        cron.target_conversation_id = conv.id
    db.add(
        Message(
            workspace_id=cron.workspace_id,
            conversation_id=conv.id,
            run_id="",
            role="user",
            content=cron.body,
        )
    )
    record_audit(
        db,
        workspace_id=cron.workspace_id,
        actor_id=cron.created_by,
        action="cron.message_posted",
        resource_type="conversation",
        resource_id=conv.id,
        detail={"cron_id": cron.id, "kind": "message"},
    )
    db.commit()


def _start_task_run(db: Session, cron: Cron) -> Optional[str]:
    """Create a fresh unattended turn for the cron's prompt, marked `cron_id`.

    Mirrors `chat.send_message`'s run creation, but the run is never started at
    chat scope: `policy_scope_for_run` reads `cron_id` and runs it at `workflow`
    scope, so a write parks for a human. With no enabled agent there is nothing
    to run the turn — the tick records that and moves on rather than raising, so
    one misconfigured workspace cannot fail the shared ticker.
    """
    agent = db.scalar(
        select(Agent)
        .where(Agent.workspace_id == cron.workspace_id, Agent.enabled.is_(True))
        .order_by(Agent.created_at, Agent.id)
    )
    if agent is None:
        logger.warning("cron %s has no enabled agent; skipping", cron.id)
        record_audit(
            db,
            workspace_id=cron.workspace_id,
            actor_id=cron.created_by,
            action="cron.skipped",
            resource_type="cron",
            resource_id=cron.id,
            detail={"reason": "no_enabled_agent", "kind": "task"},
        )
        db.commit()
        return None
    conv = Conversation(
        workspace_id=cron.workspace_id,
        created_by=cron.created_by,
        title=f"Cron: {cron.name}"[:200],
        approval_mode=conversations.default_approval_mode(
            db, workspace_id=cron.workspace_id, user_id=cron.created_by
        ),
    )
    db.add(conv)
    db.flush()
    run = Run(
        id=new_id(),
        workspace_id=cron.workspace_id,
        conversation_id=conv.id,
        agent_id=agent.id,
        created_by=cron.created_by,
        status="queued",
        prompt=cron.prompt,
        cron_id=cron.id,
    )
    message = Message(
        workspace_id=cron.workspace_id,
        conversation_id=conv.id,
        run_id=run.id,
        role="user",
        content=cron.prompt,
    )
    db.add_all([run, message])
    append_event(
        db,
        workspace_id=cron.workspace_id,
        run_id=run.id,
        event_type="run.queued",
        payload={"status": "queued", "message_id": message.id},
    )
    record_audit(
        db,
        workspace_id=cron.workspace_id,
        actor_id=cron.created_by,
        action="cron.dispatched",
        resource_type="run",
        resource_id=run.id,
        detail={"cron_id": cron.id, "kind": "task"},
    )
    db.commit()
    return run.id
