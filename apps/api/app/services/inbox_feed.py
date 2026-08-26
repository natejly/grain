"""The waiting-set queries, stated once: what waits on this member, right now.

`GET /api/inbox` and the daily digest answer the same question — "what is
waiting on this person?" — and the inbox map's standing instruction is that
the answer must not be implemented twice: a digest that reimplements the
union drifts, and a drifted digest either mails another member's personal
approvals (the roommate leak) or silently under-reports the queue (the
scan-bound bug this subsystem exists to have fixed). So the queries live
here, and both callers share them.

The contract is the inbox module's, restated:

- Waiting sets are **unbounded and oldest-first**. Only history is windowed,
  and history is not assembled here.
- Visibility on run-anchored rows is `run_activity_predicate(actor_user_id=…)`
  over a LEFT JOIN of Conversation — never an inner join, or automation rows
  vanish. Another member's personal thread never lists, which is exactly what
  keeps a digest private per member.
- Budget holds live in TWO places (`Run.paused_reason` mid-turn and
  `WorkflowRun.paused_reason` between nodes, the latter possibly with no
  backing Run at all) and are deduped on workflow_run_id.
- Mentions are personal (`target_user_id == user_id`, never ''); monitor
  alerts and spend anomalies are broadcast (`target_user_id == ''`).

Reads only. Nothing here commits, mutates, or takes a limit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AgentToolCall, Conversation, Notification, Run, Workflow, WorkflowRun
from . import conversations
from .agent_loop import PAUSED_FOR_BUDGET


@dataclass(frozen=True)
class ApprovalItem:
    """One proposed tool call whose run is parked on a decision."""

    id: str
    run_id: str
    conversation_id: str
    conversation_title: str
    name: str
    proposal_preview: str
    origin: str
    workflow_run_id: str
    workflow_id: str
    workflow_name: str
    assigned_to: str
    created_at: datetime


@dataclass(frozen=True)
class BudgetHoldItem:
    """One run (or between-nodes workflow) the spend ceiling is holding."""

    run_id: str
    conversation_id: str
    origin: str
    workflow_run_id: str
    workflow_id: str
    workflow_name: str
    created_at: datetime


@dataclass(frozen=True)
class WaitingSet:
    """Everything waiting on one member of one workspace."""

    approvals: List[ApprovalItem] = field(default_factory=list)
    budget_holds: List[BudgetHoldItem] = field(default_factory=list)
    mentions: List[Notification] = field(default_factory=list)
    alerts: List[Notification] = field(default_factory=list)
    anomalies: List[Notification] = field(default_factory=list)


def origin(*, cron_id: str, workflow_run_id: str, subject_id: str, conversation_id: str) -> str:
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


def waiting_for(db: Session, *, workspace_id: str, user_id: str) -> WaitingSet:
    """The five waiting sets, as `user_id` is entitled to see them."""
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
            AgentToolCall.workspace_id == workspace_id,
            AgentToolCall.status == "proposed",
            # A cancelled run leaves its proposal row behind; offering it as a
            # decision would 409, so it is not work and does not list.
            Run.status == "waiting_for_approval",
            conversations.run_activity_predicate(actor_user_id=user_id),
        )
        # Oldest first: the queue is answered from the top, and the top is the
        # one that has waited longest — newest-first is how a queue hides its
        # own backlog behind this morning's arrivals.
        .order_by(AgentToolCall.created_at.asc())
    ).all()
    approvals = [
        ApprovalItem(
            id=call.id,
            run_id=call.run_id,
            conversation_id=conversation_id or "",
            conversation_title=title or "",
            name=call.name,
            proposal_preview=call.proposal_preview,
            origin=origin(
                cron_id=cron_id or "",
                workflow_run_id=workflow_run_id or "",
                subject_id=subject_id or "",
                conversation_id=conversation_id or "",
            ),
            workflow_run_id=workflow_run_id or "",
            workflow_id=workflow_id or "",
            workflow_name=workflow_name or "",
            assigned_to=call.assigned_to,
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
            Run.workspace_id == workspace_id,
            Run.status == "waiting_for_approval",
            Run.paused_reason == PAUSED_FOR_BUDGET,
            conversations.run_activity_predicate(actor_user_id=user_id),
        )
        .order_by(Run.created_at.asc())
    ).all()
    budget_holds = [
        BudgetHoldItem(
            run_id=run.id,
            conversation_id=run.conversation_id or "",
            origin=origin(
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
    # first version of the feed. A workflow run is automation and so
    # member-visible by definition; no conversation predicate applies.
    seen_workflow_runs = {hold.workflow_run_id for hold in budget_holds}
    held_workflows = db.execute(
        select(WorkflowRun, Workflow.name)
        .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
        .where(
            WorkflowRun.workspace_id == workspace_id,
            WorkflowRun.status == "waiting_for_approval",
            WorkflowRun.paused_reason == PAUSED_FOR_BUDGET,
        )
        .order_by(WorkflowRun.created_at.asc())
    ).all()
    budget_holds.extend(
        BudgetHoldItem(
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
    # the member's own id rather than the `('', member)` pair the ''-targeted
    # kinds below use. Same contract as the sets above: unbounded, oldest
    # first. The composite (workspace_id, status, created_at) index narrows
    # the scan to this workspace's open rows; kind and target_user_id are not
    # in it and are filtered from that (small, self-limiting) set.
    mentions = list(
        db.scalars(
            select(Notification)
            .where(
                Notification.workspace_id == workspace_id,
                Notification.kind == "mention",
                Notification.status == "open",
                Notification.target_user_id == user_id,
            )
            .order_by(Notification.created_at.asc())
        )
    )

    # Monitor alerts are the fourth waiting set, and like budget holds they
    # are automation: written by the sweep with `target_user_id == ''`, so
    # every member sees the same list and one resolve clears it for the room.
    # The filter pins '' rather than the ('', member) pair because the sweep
    # never writes a personally-targeted alert — a row claiming to be one
    # would be a bug, not work.
    alerts = list(
        db.scalars(
            select(Notification)
            .where(
                Notification.workspace_id == workspace_id,
                Notification.kind == "monitor_alert",
                Notification.status == "open",
                Notification.target_user_id == "",
            )
            .order_by(Notification.created_at.asc())
        )
    )

    # Spend anomalies are the fifth waiting set — broadcast automation exactly
    # like monitor alerts (same '' pin, same unbounded oldest-first contract),
    # listed separately because "a line you drew was crossed" and "spending
    # drifted from its own history" are different facts asking for different
    # next steps.
    anomalies = list(
        db.scalars(
            select(Notification)
            .where(
                Notification.workspace_id == workspace_id,
                Notification.kind == "spend_anomaly",
                Notification.status == "open",
                Notification.target_user_id == "",
            )
            .order_by(Notification.created_at.asc())
        )
    )

    return WaitingSet(
        approvals=approvals,
        budget_holds=budget_holds,
        mentions=mentions,
        alerts=alerts,
        anomalies=anomalies,
    )
