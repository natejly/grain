"""The spend ceiling: what a workspace may spend on models, and who says so.

ADR 0008. `services/usage.py` made spend measurable; this makes it enforceable.
It is deliberately one module with one public predicate, because a limit
evaluated in two places is a limit that disagrees with itself.

**One predicate, checked before the call.** `evaluate` is asked *immediately
before* a model step, never after one. A post-hoc check can only record the
overspend it failed to prevent, and by then the tokens are the provider's
problem and the money is gone.

**Cost is one measurement and tokens are another.** A ceiling in dollars can only
bound spend this deployment can price, and `MODEL_PRICES` ships empty. So a
dollar ceiling standing alone over unpriced calls is not a loose limit, it is no
limit — and this module refuses to pretend otherwise: with unpriced calls in the
window and no token ceiling to bound them, the verdict is *stop*. Opting into a
ceiling is opting into being stopped, including when we cannot tell. Silence is
not permission. `Settings._guard_budget` catches the common shape of this
mistake at boot; the check here is what catches the rest — a workspace row set
through the API, or a model that turns up unpriced in an otherwise priced
deployment.

**Unset is unlimited, and stays unlimited.** Every limit defaults to None. A
workspace with no ceiling costs one indexed lookup on `workspace_budgets` per
model step and never touches the ledger — the aggregate over `model_usage`, the
expensive half, only happens once a limit exists to compare it against. A
deployment upgrading into this release must not discover a cap nobody chose.

**A failure to read the ledger must not become an outage.** `evaluate` returns
rather than raises, and its failure direction is *allow*. This is a spend
control, not a security control: the worst case of failing open is an invoice,
and the worst case of failing closed is a product that stops working because a
`SELECT` timed out. That is the same rule `record_model_usage` follows, for the
same reason — accounting never breaks a turn.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..clock import utcnow
from ..config import Settings, get_settings
from ..models import ModelUsage, WorkspaceBudget
from .usage import WORKFLOW_NODE

logger = logging.getLogger(__name__)

# Why a run was stopped. These strings reach an event payload and an audit row,
# so they are part of the contract a UI reads.
USD = "usd"
TOKENS = "tokens"
UNPRICED = "unpriced"

#: Operations that ran with nobody watching. One entry today, and a tuple rather
#: than a scalar because the *class* is the concept: any future unattended
#: operation belongs to the tighter ceiling by joining this, not by growing a
#: second code path.
UNATTENDED_OPERATIONS = (WORKFLOW_NODE,)


@dataclass(frozen=True)
class Ceiling:
    """What a workspace may spend in one window. None means no limit."""

    window_hours: int
    usd: Optional[float]
    tokens: Optional[int]
    #: `workspace` when an owner set it through the API, `settings` when it came
    #: from the deployment's configuration. Reported so an operator reading a
    #: parked run can tell which knob to turn.
    source: str

    @property
    def unlimited(self) -> bool:
        return self.usd is None and self.tokens is None

    def scaled(self, fraction: float) -> Ceiling:
        """The same ceiling, tightened — how unattended work gets a lower bar.

        Multiplicative rather than an absolute second limit, so a deployment
        that configured no ceiling still has none. There is no fraction of
        unlimited, and inventing one would be exactly the surprise cap this
        module promises not to become.
        """
        return Ceiling(
            window_hours=self.window_hours,
            usd=None if self.usd is None else self.usd * fraction,
            tokens=None if self.tokens is None else int(self.tokens * fraction),
            source=self.source,
        )


@dataclass(frozen=True)
class Spend:
    """What one window already cost. Counts only; this module reads no content."""

    calls: int
    priced_calls: int
    cost_usd: float
    total_tokens: int

    @property
    def unpriced_calls(self) -> int:
        """Calls whose model had no configured rate — spend `cost_usd` cannot see."""
        return max(0, self.calls - self.priced_calls)


NO_SPEND = Spend(calls=0, priced_calls=0, cost_usd=0.0, total_tokens=0)


@dataclass(frozen=True)
class Verdict:
    """May the next model call happen, and if not, on what evidence."""

    allowed: bool
    #: "" when allowed; otherwise USD | TOKENS | UNPRICED.
    reason: str
    #: True when the *unattended* ceiling stopped it rather than the workspace
    #: one — the difference between "this workspace has spent enough" and "your
    #: automations have".
    unattended: bool
    ceiling: Ceiling
    spend: Spend

    def payload(self) -> Dict[str, Any]:
        """The event and audit body. Every number a reader needs to act on."""
        return {
            "reason": self.reason,
            "unattended": self.unattended,
            "window_hours": self.ceiling.window_hours,
            "limit_usd": self.ceiling.usd,
            "limit_tokens": self.ceiling.tokens,
            "limit_source": self.ceiling.source,
            "spend_usd": round(self.spend.cost_usd, 6),
            "spend_tokens": self.spend.total_tokens,
            "calls": self.spend.calls,
            "unpriced_calls": self.spend.unpriced_calls,
        }

    def message(self) -> str:
        """One sentence for a human, naming the fix rather than only the fault."""
        window = f"the last {self.ceiling.window_hours}h"
        who = "Unattended workflow spend" if self.unattended else "Workspace spend"
        if self.reason == USD:
            return (
                f"{who} reached the ${self.ceiling.usd:,.2f} ceiling for {window} "
                f"(${self.spend.cost_usd:,.2f} spent). Raise the limit to continue."
            )
        if self.reason == TOKENS:
            return (
                f"{who} reached the {self.ceiling.tokens:,}-token ceiling for "
                f"{window} ({self.spend.total_tokens:,} used). Raise the limit "
                "to continue."
            )
        if self.reason == UNPRICED:
            return (
                f"{who} in {window} includes {self.spend.unpriced_calls} call(s) "
                "on models with no configured price, so the dollar ceiling cannot "
                "see them. Configure MODEL_PRICES for those models, or set a "
                "token ceiling to bound them."
            )
        return ""


def _permit(ceiling: Ceiling, spend: Spend) -> Verdict:
    """A verdict that lets the call through, carrying the evidence anyway.

    The figures travel with an allow as well as a stop so a caller that wants to
    report "you are at 80% of your ceiling" has them without asking twice.
    """
    return Verdict(
        allowed=True, reason="", unattended=False, ceiling=ceiling, spend=spend
    )


def exceeds(ceiling: Ceiling, spend: Spend) -> str:
    """**The enforcement predicate.** "" to proceed, else the reason to stop.

    Pure: a ceiling, a measurement, and no I/O — so the rule can be read, tested
    and mutated in one place instead of inferred from where it is called.

    Three clauses, in the order they matter.

    *At the boundary, stop.* `>=`, not `>`. A ceiling is a cap on cumulative
    spend, so reaching it exactly is reaching it; letting one more call through
    at the line would make every limit "the limit plus one turn", and a turn is
    unbounded in size.

    *A dollar ceiling cannot see what it cannot price.* With unpriced calls in
    the window and no token ceiling beside it, the honest verdict is stop — not
    because the workspace is over, but because nothing here can tell whether it
    is, and it was asked to.

    *Tokens are checked whether or not dollars are.* They are the ceiling that
    still works with no price list at all.
    """
    if ceiling.usd is not None:
        if spend.cost_usd >= ceiling.usd:
            return USD
        if spend.unpriced_calls > 0 and ceiling.tokens is None:
            return UNPRICED
    if ceiling.tokens is not None and spend.total_tokens >= ceiling.tokens:
        return TOKENS
    return ""


def effective_ceiling(
    db: Session, *, workspace_id: str, settings: Optional[Settings] = None
) -> Ceiling:
    """This workspace's ceiling: its own row if it has one, else the deployment's.

    Replace, not narrow. An owner who cannot raise their own ceiling cannot
    release their own parked run, and a limit whose only escape hatch is a
    redeploy is one nobody will be able to use at the moment it matters. The
    trade is stated in ADR 0008: a hosted multi-tenant deployment that needs an
    operator cap the tenant cannot lift clamps the two numbers below with
    `min()` against the settings values, and that is the only line that changes.
    """
    settings = settings or get_settings()
    row = db.scalar(
        select(WorkspaceBudget).where(WorkspaceBudget.workspace_id == workspace_id)
    )
    if row is not None:
        return Ceiling(
            window_hours=row.window_hours,
            usd=row.usd_per_window,
            tokens=row.tokens_per_window,
            source="workspace",
        )
    return Ceiling(
        window_hours=settings.budget_window_hours,
        usd=settings.budget_usd_per_window,
        tokens=settings.budget_tokens_per_window,
        source="settings",
    )


def window_spend(
    db: Session,
    *,
    workspace_id: str,
    since: datetime,
    operations: Optional[Sequence[str]] = None,
) -> Spend:
    """What this workspace spent since `since`. One indexed aggregate.

    `count(cost_usd)` ignores nulls, so it counts the calls that *have* a price
    and the remainder is what the dollar figure cannot see. Reading it that way
    rather than summing nulls as zero is the difference between a ceiling that
    knows it is partly blind and one that quietly under-reports.
    """
    filters = [
        ModelUsage.workspace_id == workspace_id,
        ModelUsage.created_at >= since,
    ]
    if operations is not None:
        filters.append(ModelUsage.operation.in_(list(operations)))
    calls, priced, cost, tokens = db.execute(
        select(
            func.count(),
            func.count(ModelUsage.cost_usd),
            func.coalesce(func.sum(ModelUsage.cost_usd), 0.0),
            func.coalesce(func.sum(ModelUsage.total_tokens), 0),
        ).where(*filters)
    ).one()
    return Spend(
        calls=int(calls),
        priced_calls=int(priced),
        cost_usd=float(cost or 0.0),
        total_tokens=int(tokens or 0),
    )


def evaluate(
    db: Session,
    *,
    workspace_id: str,
    unattended: bool,
    settings: Optional[Settings] = None,
) -> Verdict:
    """May this workspace make the model call it is about to make?

    Two ceilings, checked in that order, and an unattended call must clear both.
    The workspace ceiling bounds everything the tenant spends. The unattended one
    bounds only what ran with nobody watching, measured over its own spend rather
    than the workspace's — because a busy afternoon of human conversation should
    not stop tonight's report, and a scheduled DAG that has gone into a loop must
    be stopped even in a workspace where nobody else has spent a cent.

    Which situation a run is in is decided by the same split ADR 0007 already
    made for tool policy, and for the same reason: "nobody is watching" is a
    property of what started the run.

    Never raises. See the module docstring on why the failure direction is allow.
    """
    settings = settings or get_settings()
    try:
        ceiling = effective_ceiling(db, workspace_id=workspace_id, settings=settings)
        if ceiling.unlimited:
            # The overwhelmingly common case, and it must stay free: no query,
            # no aggregate, nothing per model step for a deployment that never
            # asked for a ceiling.
            return _permit(ceiling, NO_SPEND)

        since = utcnow() - timedelta(hours=ceiling.window_hours)
        spend = window_spend(db, workspace_id=workspace_id, since=since)
        reason = exceeds(ceiling, spend)
        if reason:
            return Verdict(
                allowed=False,
                reason=reason,
                unattended=False,
                ceiling=ceiling,
                spend=spend,
            )

        if not unattended:
            return _permit(ceiling, spend)

        tighter = ceiling.scaled(settings.unattended_budget_fraction)
        unattended_spend = window_spend(
            db,
            workspace_id=workspace_id,
            since=since,
            operations=UNATTENDED_OPERATIONS,
        )
        reason = exceeds(tighter, unattended_spend)
        if reason:
            return Verdict(
                allowed=False,
                reason=reason,
                unattended=True,
                ceiling=tighter,
                spend=unattended_spend,
            )
        return _permit(tighter, unattended_spend)
    except Exception:
        logger.warning(
            "spend ceiling could not be evaluated for workspace %s; allowing the "
            "call. A ceiling that cannot be read must not become an outage.",
            workspace_id,
            exc_info=True,
        )
        return _permit(
            Ceiling(
                window_hours=settings.budget_window_hours,
                usd=None,
                tokens=None,
                source="unavailable",
            ),
            NO_SPEND,
        )
