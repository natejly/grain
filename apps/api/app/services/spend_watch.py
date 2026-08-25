"""The spend watcher: noticing when one agent starts costing 3× its usual.

Rides the same `POST /api/workflows/tick` as every other sweep, with the same
safety story, and one structural difference: it has no rows of its own to
carry a `last_dispatched_at`, so it claims through the shared `sweep_claims`
table — one conditional-UPDATE row named ``spend_watch``, advanced to the top
of the hour, so replayed or racing ticks run the comparison at most hourly.

**What it compares.** For every (workspace, agent) with agent-attributed
model spend in the trailing 24 hours, the same 24-hour window on each of the
7 prior days is the baseline. The metric is dollars when every call involved
is priced, and total tokens the moment any call is not — an unpriced call's
cost is *unknown*, and comparing a partly-blind dollar sum against a seeing
one would flag the pricing gap, not the spend. Tokens are always measured.

**When it speaks.** Today must exceed ``max(3 × the baseline mean, a floor)``
— the floor ($1 / 100k tokens) keeps a workspace that went from two cents to
six from being paged over pennies — and the baseline must have at least 3
days with any spend at all, so a brand-new agent's first busy day is not an
"anomaly" against a history it does not have. One open ``spend_anomaly``
notification per agent: while the last one is unresolved nothing new is
written, and resolving it re-arms the watch.

**What it can do.** Read the ledger, write a notification, audit. There is no
run, no enforcement, no ceiling here — `budget.evaluate` stops runaway spend;
this notices the slow kind a ceiling is set too high to catch. Like every
sweep, a failure is a log line, never a raise out of the shared ticker.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, cast

from sqlalchemy import func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from ..clock import utcnow
from ..models import Agent, ModelUsage, Notification, SweepClaim
from .audit import record_audit
from .notifications import notify

logger = logging.getLogger(__name__)

#: The one row in `sweep_claims` this module owns.
CLAIM_NAME = "spend_watch"
#: At most one comparison per hour — spend drift is not a per-minute fact.
PERIOD = timedelta(hours=1)
#: The trailing window compared, and the width of each baseline day's window.
WINDOW = timedelta(hours=24)
#: How many prior same-width windows form the baseline...
BASELINE_DAYS = 7
#: ...and how many of them must have any spend before a comparison is honest.
MIN_BASELINE_DAYS = 3
#: Today must exceed this multiple of the baseline mean.
RATIO = 3.0
#: And clear an absolute floor, so tripled pennies stay quiet.
COST_FLOOR_USD = 1.0
TOKEN_FLOOR = 100_000


@dataclass(frozen=True)
class WindowSpend:
    """One (workspace, agent) pair's spend inside one 24-hour window."""

    calls: int
    priced_calls: int
    cost_usd: float
    total_tokens: int

    @property
    def unpriced_calls(self) -> int:
        return self.calls - self.priced_calls


def claim(db: Session, *, moment: datetime) -> bool:
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


def _window_totals(
    db: Session, *, since: datetime, until: datetime
) -> Dict[Tuple[str, str], WindowSpend]:
    """Agent-attributed spend per (workspace, agent) in [since, until).

    One grouped aggregate over the (workspace_id, created_at) index. Rows with
    no agent are background work — embeddings, compiles — with no "usual" of
    their own to drift from, so they are not watched.
    """
    rows = db.execute(
        select(
            ModelUsage.workspace_id,
            ModelUsage.agent_id,
            func.count(),
            func.count(ModelUsage.cost_usd),
            func.coalesce(func.sum(ModelUsage.cost_usd), 0.0),
            func.coalesce(func.sum(ModelUsage.total_tokens), 0),
        )
        .where(
            ModelUsage.created_at >= since,
            ModelUsage.created_at < until,
            ModelUsage.agent_id != "",
        )
        .group_by(ModelUsage.workspace_id, ModelUsage.agent_id)
    ).all()
    return {
        (str(workspace_id), str(agent_id)): WindowSpend(
            calls=int(calls),
            priced_calls=int(priced),
            cost_usd=float(cost or 0.0),
            total_tokens=int(tokens or 0),
        )
        for workspace_id, agent_id, calls, priced, cost, tokens in rows
    }


def sweep(db: Session, *, moment: Optional[datetime] = None) -> List[str]:
    """Compare every agent's day against its week; flag the outliers.

    Returns the ids of the notifications written. Never raises — one bad
    aggregate must not fail the shared ticker.
    """
    now = moment or utcnow()
    try:
        if not claim(db, moment=now):
            return []
        return _evaluate(db, now=now)
    except Exception:  # noqa: BLE001 — sweeps log, tick survives
        logger.exception("spend watch sweep failed")
        db.rollback()
        return []


def _evaluate(db: Session, *, now: datetime) -> List[str]:
    current = _window_totals(db, since=now - WINDOW, until=now)
    if not current:
        return []
    baselines = [
        _window_totals(
            db,
            since=now - WINDOW * (day + 1),
            until=now - WINDOW * day,
        )
        for day in range(1, BASELINE_DAYS + 1)
    ]
    flagged: List[str] = []
    for (workspace_id, agent_id), today in sorted(current.items()):
        days = [
            baseline[(workspace_id, agent_id)]
            for baseline in baselines
            if (workspace_id, agent_id) in baseline
            and baseline[(workspace_id, agent_id)].calls > 0
        ]
        if len(days) < MIN_BASELINE_DAYS:
            # A history too thin to have a "usual" cannot honestly be
            # deviated from. Silence, not a guess.
            continue
        # Dollars only when every call in sight is priced; otherwise the
        # dollar sums are partly blind and tokens are the honest measure.
        unpriced_anywhere = today.unpriced_calls > 0 or any(
            day.unpriced_calls > 0 for day in days
        )
        if unpriced_anywhere:
            observed = float(today.total_tokens)
            baseline_mean = sum(day.total_tokens for day in days) / len(days)
            floor, unit = float(TOKEN_FLOOR), "tokens"
        else:
            observed = today.cost_usd
            baseline_mean = sum(day.cost_usd for day in days) / len(days)
            floor, unit = COST_FLOOR_USD, "USD"
        if observed <= max(RATIO * baseline_mean, floor):
            continue
        already_open = db.scalar(
            select(Notification.id).where(
                Notification.workspace_id == workspace_id,
                Notification.kind == "spend_anomaly",
                Notification.status == "open",
                Notification.agent_id == agent_id,
            )
        )
        if already_open is not None:
            # The room has been told and has not answered; repeating it every
            # hour teaches everyone to mute the channel. Resolving re-arms.
            continue
        # Resolved under the SPENDING workspace, so a stale id (a deleted
        # agent whose ledger rows outlive it) degrades to the id, never to a
        # foreign workspace's agent name.
        name = db.scalar(
            select(Agent.name).where(
                Agent.id == agent_id, Agent.workspace_id == workspace_id
            )
        )
        notification = notify(
            db,
            workspace_id=workspace_id,
            kind="spend_anomaly",
            # '' = every member: spend is a workspace fact, like a monitor
            # trip, and the first member to resolve has answered for the room.
            target_user_id="",
            agent_id=agent_id,
            title=f"{name or agent_id} is at 3× its usual spend",
            body=(
                f"About {_amount(observed, unit)} in the last 24 hours, against "
                f"a usual {_amount(baseline_mean, unit)} over the prior "
                f"{len(days)} active days."
            ),
        )
        record_audit(
            db,
            workspace_id=workspace_id,
            # The ticker has no user; automation audits as nobody, the way an
            # expired-invite purge would.
            actor_id="",
            action="spend.anomaly_flagged",
            resource_type="agent",
            resource_id=agent_id,
            detail={
                "observed": round(observed, 6),
                "baseline_mean": round(baseline_mean, 6),
                "unit": unit,
                "baseline_days": len(days),
            },
        )
        # Committed per flag, like the monitor sweep: one workspace's write
        # must not hold a transaction open across every other workspace's
        # comparison, and a later failure must not roll back an alert that
        # already earned its existence.
        db.commit()
        flagged.append(notification.id)
    return flagged


def _amount(value: float, unit: str) -> str:
    """"$2.40" or "150,000 tokens" — the two units this module compares in."""
    if unit == "USD":
        return f"${value:,.2f}"
    return f"{value:,.0f} tokens"
