"""The monitor ticker: asking a stored threshold question, unattended.

A sibling of `services/crons.py`, folded into the same `POST /api/workflows/tick`
behind the same secret, with the same safety story:

**It is a claim, not a trigger.** `monitors.last_dispatched_at` is advanced by a
conditional UPDATE (`claim`), so a tick replayed, retried, or racing another
instance evaluates each monitor once per minute.

**It cannot reach into the past.** Only the just-missed minute fires (`CATCHUP`);
older minutes are dropped — a monitor that catches up on a day of downtime
would page a day of stale alerts.

**It grants nothing and executes nothing.** An evaluation is a *read*: the
monitor's stored `DatasetQuery` runs against its dataset, resolved under the
monitor's own workspace, and the only write is a `monitor_alert` notification on
the ok→tripped edge. There is no run, no agent turn, no policy scope to get
wrong, because nothing here can act.

**One bad monitor cannot fail the shared ticker.** Every failure — a purged or
foreign dataset, a query the schema no longer satisfies, a non-numeric value —
is a skip with a `monitor.skipped` audit, never a raise.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, cast

from sqlalchemy import or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from ..clock import utcnow
from ..models import Monitor, Notification
from ..schemas import DatasetQuery
from .analytics import AnalyticsValidationError, execute_dataset_query
from .audit import record_audit
from .notifications import notify
from .workflows import schedule
from .workflows.validate import cron_matches

logger = logging.getLogger(__name__)

#: One minute of grace, exactly as the workflow and cron tickers have it.
CATCHUP = schedule.CATCHUP

#: How each comparator trips an observed value against the threshold.
COMPARATORS: Dict[str, Callable[[float, float], bool]] = {
    "gt": lambda value, threshold: value > threshold,
    "lt": lambda value, threshold: value < threshold,
    "gte": lambda value, threshold: value >= threshold,
    "lte": lambda value, threshold: value <= threshold,
}


@dataclass(frozen=True)
class EvalOutcome:
    """What one evaluation concluded, whoever asked for it.

    `state` is "ok" | "tripped" | "skipped"; a skip names its `reason` and
    leaves the monitor's stored edge state untouched, so a transient failure
    can never swallow the next genuine trip.
    """

    state: str
    value_json: str
    reason: str


def due(monitor: Monitor, *, moment: datetime) -> bool:
    """Is this monitor scheduled to evaluate in the minute `moment` falls in?"""
    if not monitor.enabled:
        return False
    local = schedule.local_moment(moment, monitor.schedule_timezone)
    if local is None:
        logger.warning(
            "monitor %s has an unknown timezone %r; not dispatching",
            monitor.id,
            monitor.schedule_timezone,
        )
        return False
    return cron_matches(monitor.schedule_cron, local)


def claim(db: Session, monitor: Monitor, *, minute: datetime) -> bool:
    """Advance `last_dispatched_at` to `minute`, once. True means we won it."""
    result = cast(
        "CursorResult[Any]",
        db.execute(
            update(Monitor)
            .where(
                Monitor.id == monitor.id,
                or_(
                    Monitor.last_dispatched_at.is_(None),
                    Monitor.last_dispatched_at < minute,
                ),
            )
            .values(last_dispatched_at=minute)
        ),
    )
    db.commit()
    return bool(result.rowcount == 1)


def dispatch_due(db: Session, *, moment: Optional[datetime] = None) -> List[str]:
    """Claim and evaluate every enabled monitor due now.

    Returns the ids of the monitors evaluated (including skips — the id names
    the monitor the tick spent time on, not a verdict). Evaluation is a bounded
    read, so unlike a cron's task run there is nothing to enqueue.
    """
    now = schedule.floor_minute(moment or utcnow())
    candidates = list(
        db.scalars(
            select(Monitor).where(
                Monitor.enabled.is_(True),
                or_(
                    Monitor.last_dispatched_at.is_(None),
                    Monitor.last_dispatched_at < now,
                ),
            )
        )
    )
    evaluated: List[str] = []
    for monitor in candidates:
        minute = _firing_minute(monitor, now)
        if minute is None:
            continue
        if not claim(db, monitor, minute=minute):
            continue
        evaluate(db, monitor)
        evaluated.append(monitor.id)
    db.commit()
    return evaluated


def _firing_minute(monitor: Monitor, now: datetime) -> Optional[datetime]:
    """The minute this monitor is due for, or None — `crons._firing_minute`."""
    for offset in range(int(CATCHUP.total_seconds() // 60) + 1):
        candidate = now - timedelta(minutes=offset)
        if (
            monitor.last_dispatched_at is not None
            and candidate <= monitor.last_dispatched_at
        ):
            break
        if due(monitor, moment=candidate):
            return candidate
    return None


def evaluate(db: Session, monitor: Monitor, *, actor_id: str = "") -> EvalOutcome:
    """Run the monitor's question once and record what came back.

    Called by `dispatch_due` (after the atomic claim) and by run-now (claim-free
    — a person is asking now). Commits its own work, like `crons.fire`, so a
    sweep transaction never spans monitors.

    The edge rule: an alert is written only when a tripped evaluation follows a
    state that was not "tripped" — the very first evaluation included, because
    '' (never evaluated) is not "ok" but is also not already alerted on. While
    the value stays over the line nothing new is written; a recovery back to
    "ok" re-arms the edge.
    """
    who = actor_id or monitor.created_by
    try:
        query = DatasetQuery.model_validate(json.loads(monitor.query_json or "{}"))
        if not query.metrics:
            return _skip(db, monitor, actor_id=who, reason="query has no metric")
        result = execute_dataset_query(
            db,
            # The monitor's OWN workspace, never a caller's: a foreign or purged
            # dataset_id resolves to nothing here and becomes a skip.
            workspace_id=monitor.workspace_id,
            dataset_id=monitor.dataset_id,
            query=query,
        )
        if not result.rows:
            return _skip(db, monitor, actor_id=who, reason="query returned no rows")
        value = result.rows[0].get(query.metrics[0].label)
        # DuckDB hands DECIMAL aggregates back as decimal.Decimal — a number in
        # every sense that matters here, so it compares like one.
        if isinstance(value, Decimal):
            value = float(value)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return _skip(
                db, monitor, actor_id=who, reason="metric value is not a number"
            )
        # NaN passes the isinstance check but answers False to every comparator,
        # and json.dumps would write the invalid-JSON literal `NaN` into
        # last_value_json. Infinities dump the same way. Neither is a value a
        # threshold can honestly judge — skip, exactly like a non-number.
        if isinstance(value, float) and not math.isfinite(value):
            return _skip(
                db, monitor, actor_id=who, reason="metric value is not a finite number"
            )
        compare = COMPARATORS.get(monitor.comparator)
        if compare is None:
            return _skip(db, monitor, actor_id=who, reason="unknown comparator")
        tripped = compare(float(value), monitor.threshold)
        value_json = json.dumps(value)
        state = "tripped" if tripped else "ok"
        if tripped and monitor.last_state != "tripped" and not _open_alert_exists(
            db, monitor
        ):
            notify(
                db,
                workspace_id=monitor.workspace_id,
                kind="monitor_alert",
                # '' = every member: an alert is automation, member-visible by
                # definition, exactly like a budget hold.
                target_user_id="",
                title=(
                    f"{monitor.name}: value {value} crossed threshold "
                    f"{monitor.threshold}"
                ),
                body=(
                    f"Observed {value}, configured to alert when the value is "
                    f"{monitor.comparator} {monitor.threshold}."
                ),
                monitor_id=monitor.id,
                dashboard_id="",
            )
            record_audit(
                db,
                workspace_id=monitor.workspace_id,
                actor_id=who,
                action="monitor.tripped",
                resource_type="monitor",
                resource_id=monitor.id,
                detail={
                    "value": value,
                    "threshold": monitor.threshold,
                    "comparator": monitor.comparator,
                },
            )
        monitor.last_state = state
        monitor.last_value_json = value_json
        db.commit()
        return EvalOutcome(state=state, value_json=value_json, reason="")
    except Exception as exc:  # noqa: BLE001 — one bad monitor must not fail the tick
        db.rollback()
        if not isinstance(exc, (AnalyticsValidationError, ValueError)):
            logger.exception("monitor %s evaluation failed", monitor.id)
        return _skip(db, monitor, actor_id=who, reason=str(exc) or "query failed")


def _open_alert_exists(db: Session, monitor: Monitor) -> bool:
    """Is an unresolved alert for this monitor already in the Inbox?

    Two dedups in one read. First, the race: run-now is claim-free, so it can
    evaluate the same crossing concurrently with the tick — both see a stale
    `last_state`, and without this check both would notify. Second, the unacked
    edge: while an alert nobody has resolved still badges every Inbox, a
    recovery-and-recross has nothing new to tell the room — one open row per
    monitor is the whole contract. The re-alert in
    `test_the_edge_realerts_only_after_a_recovery` therefore requires the
    earlier alert to have been acknowledged.
    """
    return (
        db.scalar(
            select(Notification.id).where(
                Notification.workspace_id == monitor.workspace_id,
                Notification.kind == "monitor_alert",
                Notification.monitor_id == monitor.id,
                Notification.status == "open",
            )
        )
        is not None
    )


def _skip(db: Session, monitor: Monitor, *, actor_id: str, reason: str) -> EvalOutcome:
    """Record that this evaluation could not answer, and move on.

    `last_state` is deliberately untouched: a skip is "we do not know", and
    overwriting a stored "tripped" with it would re-arm the edge and duplicate
    the alert once the query works again.
    """
    logger.warning("monitor %s skipped: %s", monitor.id, reason)
    record_audit(
        db,
        workspace_id=monitor.workspace_id,
        actor_id=actor_id,
        action="monitor.skipped",
        resource_type="monitor",
        resource_id=monitor.id,
        detail={"reason": reason[:300]},
    )
    db.commit()
    return EvalOutcome(state="skipped", value_json="", reason=reason[:300])
