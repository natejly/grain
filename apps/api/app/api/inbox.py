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
from ..models import AgentToolCall, Conversation, Notification, Run, Workflow, WorkflowRun
from ..schemas import ApiModel
from ..services import conversations
from ..services.agent_loop import PAUSED_FOR_BUDGET

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
    recent_runs: List[InboxRunOut]


def _origin(*, cron_id: str, workflow_run_id: str, subject_id: str, conversation_id: str) -> str:
    """Where the parked run came from, in the order a reader would ask.

    Workflow outranks schedule on purpose: a scheduled workflow's park should
    send the reader to the workflow run (where the node and its inputs are),
    not to a cron page that only knows when it fired.
    """
    if workflow_run_id:
        return "workflow"
    if cron_id:
        return "schedule"
    if subject_id:
        return "subject"
    if conversation_id:
        return "chat"
    return "chat"


@router.get("", response_model=InboxOut)
def read_inbox(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> InboxOut:
    """The attention feed: parked approvals and budget holds, then history."""
    rows = db.execute(
        select(
            AgentToolCall,
            Run.conversation_id,
            Run.cron_id,
            WorkflowRun.id,
            WorkflowRun.workflow_id,
            Workflow.name,
            Conversation.subject_id,
            Conversation.title,
        )
        .join(Run, Run.id == AgentToolCall.run_id)
        # LEFT JOINs so automation with no chat thread still lists.
        .outerjoin(Conversation, Conversation.id == Run.conversation_id)
        .outerjoin(WorkflowRun, WorkflowRun.run_id == Run.id)
        .outerjoin(Workflow, Workflow.id == WorkflowRun.workflow_id)
        .where(
            AgentToolCall.workspace_id == actor.workspace_id,
            AgentToolCall.status == "proposed",
            # A cancelled run leaves its proposal row behind; offering it as a
            # decision would 409, so it is not work and does not list.
            Run.status == "waiting_for_approval",
            conversations.run_activity_predicate(actor_user_id=actor.user_id),
        )
        # Oldest first: the queue is answered from the top, and the top is the
        # one that has waited longest — newest-first is how a queue hides its
        # own backlog behind this morning's arrivals.
        .order_by(AgentToolCall.created_at.asc())
    ).all()
    approvals = [
        InboxApprovalOut(
            id=call.id,
            run_id=call.run_id,
            conversation_id=conversation_id or "",
            conversation_title=title or "",
            name=call.name,
            proposal_preview=call.proposal_preview,
            origin=_origin(
                cron_id=cron_id or "",
                workflow_run_id=workflow_run_id or "",
                subject_id=subject_id or "",
                conversation_id=conversation_id or "",
            ),
            workflow_run_id=workflow_run_id or "",
            workflow_id=workflow_id or "",
            workflow_name=workflow_name or "",
            created_at=call.created_at,
        )
        for (
            call,
            conversation_id,
            cron_id,
            workflow_run_id,
            workflow_id,
            workflow_name,
            subject_id,
            title,
        ) in rows
    ]

    held = db.execute(
        select(Run, WorkflowRun.id, WorkflowRun.workflow_id, Workflow.name)
        .outerjoin(Conversation, Conversation.id == Run.conversation_id)
        .outerjoin(WorkflowRun, WorkflowRun.run_id == Run.id)
        .outerjoin(Workflow, Workflow.id == WorkflowRun.workflow_id)
        .where(
            Run.workspace_id == actor.workspace_id,
            Run.status == "waiting_for_approval",
            Run.paused_reason == PAUSED_FOR_BUDGET,
            conversations.run_activity_predicate(actor_user_id=actor.user_id),
        )
        .order_by(Run.created_at.asc())
    ).all()
    budget_holds = [
        InboxBudgetHoldOut(
            run_id=run.id,
            conversation_id=run.conversation_id or "",
            origin=_origin(
                cron_id=run.cron_id or "",
                workflow_run_id=workflow_run_id or "",
                subject_id="",
                conversation_id=run.conversation_id or "",
            ),
            workflow_run_id=workflow_run_id or "",
            workflow_id=workflow_id or "",
            workflow_name=workflow_name or "",
            created_at=run.created_at,
        )
        for run, workflow_run_id, workflow_id, workflow_name in held
    ]

    # A workflow the ceiling stopped between nodes carries the park on its OWN
    # row — `WorkflowRun.paused_reason` — and may have no backing chat Run at
    # all (run_id is null until a node needs one). The Run-anchored query above
    # cannot see those, which is exactly how a held workflow vanished from the
    # first version of this feed. A workflow run is automation and so
    # member-visible by definition; no conversation predicate applies.
    seen_workflow_runs = {hold.workflow_run_id for hold in budget_holds}
    held_workflows = db.execute(
        select(WorkflowRun, Workflow.name)
        .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
        .where(
            WorkflowRun.workspace_id == actor.workspace_id,
            WorkflowRun.status == "waiting_for_approval",
            WorkflowRun.paused_reason == PAUSED_FOR_BUDGET,
        )
        .order_by(WorkflowRun.created_at.asc())
    ).all()
    budget_holds.extend(
        InboxBudgetHoldOut(
            run_id=workflow_run.run_id or "",
            conversation_id="",
            origin="workflow",
            workflow_run_id=workflow_run.id,
            workflow_id=workflow_run.workflow_id,
            workflow_name=name,
            created_at=workflow_run.created_at,
        )
        for workflow_run, name in held_workflows
        if workflow_run.id not in seen_workflow_runs
    )
    budget_holds.sort(key=lambda hold: hold.created_at)

    # Mentions are the third waiting set, and the first personal one: an open
    # `kind='mention'` notification is work for exactly the member it names —
    # `target_user_id` is never '' for a mention — which is why the filter is
    # the actor's own id rather than the `('', actor)` pair the ''-targeted
    # kinds (monitor alerts, spend anomalies) will use. Same contract as the
    # sets above: unbounded, oldest first, over the composite
    # (workspace_id, status, created_at) index.
    open_mentions = db.scalars(
        select(Notification)
        .where(
            Notification.workspace_id == actor.workspace_id,
            Notification.kind == "mention",
            Notification.status == "open",
            Notification.target_user_id == actor.user_id,
        )
        .order_by(Notification.created_at.asc())
    ).all()
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
        for row in open_mentions
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
        recent_runs=recent_runs,
    )
