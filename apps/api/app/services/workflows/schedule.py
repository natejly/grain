"""The schedule ticker: turning a stored cron into a dispatched run.

ADR 0007 left this out and said why the gap mattered — "a stored schedule is a
recorded intent", and a UI describing it as active would be making a promise the
system does not keep. This closes it, and the shape is chosen to avoid adding an
always-on service to a product that does not have one: an external cron (Vercel
Cron, a Kubernetes CronJob, a `curl` in a crontab) calls
`POST /api/workflows/tick` every minute and this module decides what is due.

Three properties make that safe enough to hand to an unauthenticated caller
holding a shared secret:

**It is a claim, not a trigger.** The caller cannot say *which* workflow to run
and cannot say *when* it is. `workflows.last_dispatched_at` is advanced by a
conditional UPDATE, so a tick replayed, retried, or delivered to three instances
at once produces one run per workflow per minute. Calling it a hundred times in
one minute is indistinguishable from calling it once.

**It cannot reach into the past.** The moment is the server's clock, never the
request's. A caller who could pass a timestamp could replay every Monday in the
year and fire a weekly workflow fifty-two times.

**It grants nothing.** A dispatched run executes at `workflow` policy scope like
any other, so the first write-capable node parks and waits for a human. The
ticker starts work; it never authorises it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from ...clock import utcnow
from ...models import Workflow, WorkflowRun
from . import executor
from .validate import cron_error, cron_matches

logger = logging.getLogger(__name__)

#: A tick that arrives late must still fire the minute it was late for, but it
#: must not replay an afternoon. One minute of grace covers a slow request and
#: nothing else.
CATCHUP = timedelta(minutes=1)


def floor_minute(moment: datetime) -> datetime:
    """The claim's granularity. Cron's resolution is a minute; so is the claim."""
    return moment.replace(second=0, microsecond=0)


def local_moment(moment: datetime, zone: str) -> Optional[datetime]:
    """`moment` (naive UTC) as a naive wall clock in `zone`.

    None for a timezone this host does not know, which the caller treats as "do
    not dispatch". Guessing UTC instead would run a workflow at the wrong hour
    and never say so.
    """
    try:
        target = ZoneInfo(zone or "UTC")
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None
    return moment.replace(tzinfo=timezone.utc).astimezone(target).replace(tzinfo=None)


#: How far ahead `next_fires` will look. A 5-field cron that recurs at all
#: recurs within a year — except a Feb 29 schedule, whose next fire can be four
#: years out; previewing that as "no upcoming fires" is honest, and cheaper than
#: teaching every caller to wait out a four-year scan.
SCAN_DAYS = 370


def next_fires(
    expression: str,
    timezone_name: str,
    *,
    count: int = 3,
    now: Optional[datetime] = None,
) -> List[datetime]:
    """The next `count` UTC minutes this cron fires in `timezone_name`.

    A preview, so it answers exactly as the ticker would: step forward through
    UTC minutes and ask whether each minute's *local* wall clock matches — which
    is how a spring-forward gap skips a fire and a fall-back hour fires twice,
    here as in dispatch. Returns naive-UTC datetimes (the storage convention),
    and fewer than `count` — possibly none — when the horizon runs out first.
    An invalid cron or unknown timezone answers [] rather than raising: the
    validator owns the refusal and its message; this function only knows time.
    """
    if count <= 0 or cron_error(expression) is not None:
        return []
    fields = expression.strip().split()
    # The cron grammar factors: fields 1-2 constrain the wall *clock*, fields
    # 3-5 the wall *date* (including the POSIX dom/dow OR, which lives wholly
    # inside the date fields), and a minute fires iff both halves match. Split
    # the expression so a dead day is dismissed with one call at whatever time
    # the scan happens to hold, instead of 1440 — the difference between an
    # impossible cron costing a few hundred matches and half a million —
    # while `cron_matches` stays the only code that knows what a field means.
    day_expr = " ".join(("*", "*", fields[2], fields[3], fields[4]))
    time_expr = " ".join((fields[0], fields[1], "*", "*", "*"))
    candidate = floor_minute(now if now is not None else utcnow()) + timedelta(
        minutes=1
    )
    horizon = candidate + timedelta(days=SCAN_DAYS)
    fires: List[datetime] = []
    while candidate < horizon and len(fires) < count:
        local = local_moment(candidate, timezone_name)
        if local is None:
            return []
        if not cron_matches(day_expr, local):
            # Jump toward the next local midnight, stopping 61 minutes short: a
            # DST shift in between makes the UTC jump overshoot the local date
            # line by up to an hour, and overshooting would skip real fires.
            # The tail is walked minute by minute, which is cheap and exact.
            next_midnight = local.replace(
                hour=0, minute=0, second=0, microsecond=0
            ) + timedelta(days=1)
            skip = int((next_midnight - local).total_seconds() // 60) - 61
            candidate += timedelta(minutes=max(skip, 1))
            continue
        if cron_matches(time_expr, local):
            fires.append(candidate)
        candidate += timedelta(minutes=1)
    return fires


def due(workflow: Workflow, *, moment: datetime) -> bool:
    """Is this workflow scheduled to fire in the minute `moment` falls in?"""
    if workflow.status != "active" or workflow.trigger_kind != "schedule":
        return False
    local = local_moment(moment, workflow.schedule_timezone)
    if local is None:
        logger.warning(
            "workflow %s has an unknown timezone %r; not dispatching",
            workflow.id,
            workflow.schedule_timezone,
        )
        return False
    return cron_matches(workflow.schedule_cron, local)


def claim(db: Session, workflow: Workflow, *, minute: datetime) -> bool:
    """Advance `last_dispatched_at` to `minute`, once. True means we won it.

    The whole of the at-most-once guarantee is this one conditional UPDATE.
    Reading the column and then writing it would be a race with a width equal to
    however long the read took; letting the database compare and swap makes the
    width zero, and the loser of the race gets `rowcount == 0` and moves on.
    """
    # `Session.execute` is typed as returning `Result`, which has no rowcount;
    # a DML statement always yields a `CursorResult`, which does.
    result = cast(
        "CursorResult[Any]",
        db.execute(
            update(Workflow)
            .where(
                Workflow.id == workflow.id,
                or_(
                    Workflow.last_dispatched_at.is_(None),
                    Workflow.last_dispatched_at < minute,
                ),
            )
            .values(last_dispatched_at=minute)
        ),
    )
    db.commit()
    return bool(result.rowcount == 1)


def dispatch_due(db: Session, *, moment: Optional[datetime] = None) -> List[WorkflowRun]:
    """Claim and start every workflow due now. Returns the runs it created.

    Runs are only *created* here, not executed: the caller starts them on a
    background task so one slow workflow cannot make the tick time out and a
    retrying cron cannot pile up duplicate requests behind it.
    """
    now = floor_minute(moment or utcnow())
    candidates = list(
        db.scalars(
            select(Workflow).where(
                Workflow.status == "active",
                Workflow.trigger_kind == "schedule",
                or_(
                    Workflow.last_dispatched_at.is_(None),
                    Workflow.last_dispatched_at < now,
                ),
            )
        )
    )
    started: List[WorkflowRun] = []
    for workflow in candidates:
        minute = _firing_minute(workflow, now)
        if minute is None:
            continue
        if not claim(db, workflow, minute=minute):
            continue
        started.append(
            executor.start_run(
                db,
                workflow,
                user_id=workflow.created_by,
                trigger="schedule",
                payload={"scheduled_for": minute.isoformat()},
            )
        )
    db.commit()
    return started


def _firing_minute(workflow: Workflow, now: datetime) -> Optional[datetime]:
    """The minute this workflow is due for, or None.

    `CATCHUP` covers the previous minute so a tick delayed past the boundary
    still fires it — but only if that minute was not already claimed, which the
    conditional UPDATE decides. Anything older is deliberately dropped: a
    scheduler that catches up on a day of downtime sends a day of email.
    """
    for offset in range(int(CATCHUP.total_seconds() // 60) + 1):
        candidate = now - timedelta(minutes=offset)
        if workflow.last_dispatched_at is not None and (
            candidate <= workflow.last_dispatched_at
        ):
            break
        if due(workflow, moment=candidate):
            return candidate
    return None
