"""Everything waiting on a person, in one list, with no scan bound.

The queue used to be assembled from four surfaces with four different windows:
`GET /api/agent-tool-calls` returns the last fifty calls of ANY status, so
fifty recent searches push a genuinely parked approval off the list; the
workflows page scanned the first twenty-five workflows' last twenty runs each;
budget holds were owner-only on `/api/admin/budget`; and pending document
edits had a fifty-row window of their own. Each bound was individually
reasonable and together they meant a surface could say "nothing waiting" while
a run sat parked — the exact lie an approval queue exists to prevent.

This endpoint's contract is therefore the opposite: **what waits on a human is
listed without a limit.** The waiting set is small by nature — every entry is
a run the loop has stopped — so an unbounded query over an indexed
(workspace, status) predicate is cheap, and a cap would reintroduce the bug in
exchange for nothing. Only the *history* section (recent workflow outcomes,
which nothing is blocked on) is windowed.

Visibility matches the rest of the run surfaces: `run_activity_predicate`, so
automation (cron, workflow, subject threads) and shared threads list for every
member, and another member's personal thread never does. Budget holds were
owner-only when they lived on the admin page; here the *existence* of a held
run is member-visible under the same predicate — a teammate who cannot see
that the ceiling stopped the nightly digest cannot know to ask an owner to
raise it — while the ceiling's numbers and the release lever stay owner-only
on `/api/admin/budget`.

Decisions are not taken here: approvals resolve through the one existing
`POST /api/agent-tool-calls/{id}/decision`, whatever origin parked them, and a
budget hold is released by an owner raising the ceiling. This router only ever
reads, which is why nothing in it takes an Idempotency-Key.
"""
from __future__ import annotations

from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..database import get_db
from ..models import Workflow, WorkflowRun
from ..schemas import ApiModel
from ..services import inbox_feed

router = APIRouter(prefix="/api/inbox", tags=["inbox"])

#: The one window in this module, and it is on history: recent workflow
#: outcomes are context, not work, so nobody is blocked by the 51st.
RECENT_OUTCOMES = 50


class InboxApprovalOut(ApiModel):
    """One proposed tool call whose run is parked on the decision."""

    id: str
    run_id: str
    #: "" for automation with no chat thread; otherwise the deep-link target.
    conversation_id: str
    #: The thread's title, for a human-readable "where" ("" for automation).
    conversation_title: str
    name: str
    proposal_preview: str
    #: chat | subject | workflow | schedule — where the parked run came from,
    #: which is what decides the deep link the client offers.
    origin: str
    #: Set when a workflow parked it, so the client can link to the run detail.
    workflow_run_id: str
    workflow_id: str
    #: The workflow's display name, "" for every other origin — so the client
    #: can say "Weekly digest wants to run edit_document" without a second
    #: request per row.
    workflow_name: str
    #: The member this approval is routed to, "" for anyone. Deliberately NOT a
    #: server-side filter: nothing parked is invisible — the client de-emphasizes
    #: rows assigned to someone else rather than this feed hiding them.
    assigned_to: str
    created_at: datetime


class InboxBudgetHoldOut(ApiModel):
    """One run the spend ceiling is holding. No approve/deny exists for these:
    there is no proposed call, and release is an owner raising the ceiling."""

    run_id: str
    conversation_id: str
    origin: str
    workflow_run_id: str
    workflow_id: str
    workflow_name: str
    created_at: datetime


class InboxMentionOut(ApiModel):
    """One open @mention of the caller. Personal by definition — the query
    below filters on `target_user_id == actor.user_id`, never '' — so one
    member's mentions are invisible to their roommate."""

    id: str
    title: str
    body: str
    #: Deep-link columns, '' when the subject is of another kind.
    conversation_id: str
    document_id: str
    dashboard_id: str
    comment_id: str
    created_by: str
    created_at: datetime


class InboxAlertOut(ApiModel):
    """One open monitor alert. Automation, so member-visible by definition —
    the query below filters on `target_user_id == ''` (the monitor sweep only
    ever writes '' -targeted rows), the holds-tab pattern: no conversation
    predicate, every member sees it, and resolving it resolves it for all."""

    id: str
    title: str
    body: str
    #: Deep link to the monitor that tripped, for the Monitors view.
    monitor_id: str
    created_at: datetime


class InboxAnomalyOut(ApiModel):
    """One open spend anomaly: an agent running well over its usual spend.

    Broadcast like a monitor alert — the sweep writes only '' -targeted rows,
    every member sees the same list, one resolve clears it for the room. Its
    own list rather than a fifth kind folded into `alerts`, because the two
    mean different things: an alert says a number you chose crossed a line you
    drew; an anomaly says spending drifted from its own history, unasked."""

    id: str
    title: str
    body: str
    #: The agent whose spend drifted — the deep link's subject. Historical id:
    #: the agent may since have been deleted, and the row still means what it
    #: meant.
    agent_id: str
    created_at: datetime


class InboxRunOut(ApiModel):
    """One finished workflow run — the Inbox's history shelf, not its work."""

    id: str
    workflow_id: str
    workflow_name: str
    status: str
    error: str
    created_at: datetime


class InboxOut(ApiModel):
    approvals: List[InboxApprovalOut]
    budget_holds: List[InboxBudgetHoldOut]
    mentions: List[InboxMentionOut]
    alerts: List[InboxAlertOut]
    anomalies: List[InboxAnomalyOut]
    recent_runs: List[InboxRunOut]


@router.get("", response_model=InboxOut)
def read_inbox(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> InboxOut:
    """The attention feed: parked approvals and budget holds, then history.

    The waiting sets come from `services/inbox_feed.waiting_for` — the ONE
    implementation of "what waits on this member", shared with the daily
    digest so the two surfaces cannot drift (see that module's docstring).
    This router only shapes the answer and appends the windowed history.
    """
    waiting = inbox_feed.waiting_for(
        db, workspace_id=actor.workspace_id, user_id=actor.user_id
    )
    approvals = [
        InboxApprovalOut(
            id=item.id,
            run_id=item.run_id,
            conversation_id=item.conversation_id,
            conversation_title=item.conversation_title,
            name=item.name,
            proposal_preview=item.proposal_preview,
            origin=item.origin,
            workflow_run_id=item.workflow_run_id,
            workflow_id=item.workflow_id,
            workflow_name=item.workflow_name,
            assigned_to=item.assigned_to,
            created_at=item.created_at,
        )
        for item in waiting.approvals
    ]
    budget_holds = [
        InboxBudgetHoldOut(
            run_id=item.run_id,
            conversation_id=item.conversation_id,
            origin=item.origin,
            workflow_run_id=item.workflow_run_id,
            workflow_id=item.workflow_id,
            workflow_name=item.workflow_name,
            created_at=item.created_at,
        )
        for item in waiting.budget_holds
    ]
    mentions = [
        InboxMentionOut(
            id=row.id,
            title=row.title,
            body=row.body,
            conversation_id=row.conversation_id,
            document_id=row.document_id,
            dashboard_id=row.dashboard_id,
            comment_id=row.comment_id,
            created_by=row.created_by,
            created_at=row.created_at,
        )
        for row in waiting.mentions
    ]
    alerts = [
        InboxAlertOut(
            id=row.id,
            title=row.title,
            body=row.body,
            monitor_id=row.monitor_id,
            created_at=row.created_at,
        )
        for row in waiting.alerts
    ]
    anomalies = [
        InboxAnomalyOut(
            id=row.id,
            title=row.title,
            body=row.body,
            agent_id=row.agent_id,
            created_at=row.created_at,
        )
        for row in waiting.anomalies
    ]

    outcomes = db.execute(
        select(WorkflowRun, Workflow.name)
        .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
        .where(
            WorkflowRun.workspace_id == actor.workspace_id,
            WorkflowRun.status.in_(("succeeded", "failed")),
        )
        .order_by(WorkflowRun.created_at.desc())
        .limit(RECENT_OUTCOMES)
    ).all()
    recent_runs = [
        InboxRunOut(
            id=run.id,
            workflow_id=run.workflow_id,
            workflow_name=name,
            status=run.status,
            error=run.error,
            created_at=run.created_at,
        )
        for run, name in outcomes
    ]

    return InboxOut(
        approvals=approvals,
        budget_holds=budget_holds,
        mentions=mentions,
        alerts=alerts,
        anomalies=anomalies,
        recent_runs=recent_runs,
    )
