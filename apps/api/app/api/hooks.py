"""Token-authenticated inbound hooks: the two things a machine may do here.

Both routes resolve identity through `auth.get_token_actor` — an
`Authorization: Bearer grain_…` workspace API token — never a cookie, so they
sit in PUBLIC_UNSAFE_ROUTES (the tripwire looks for `get_actor` specifically)
with targeted tests proving a token reaches exactly its own workspace.

The surface is deliberately this small:

**Trigger a workflow.** The webhook classic. The run is a `WorkflowRun`, so
`policy_scope_for_run` classifies every tool call in it at *workflow* scope by
construction — a standing chat "always allow" does not authorise what this
route starts, and an unattended write parks for a human exactly as a
scheduled run's would.

**Post a note into a thread.** An external system leaving a message
(`run_id=""`, the `crons._post_message` shape) in a conversation the token's
member can see. It does NOT start an agent turn: a machine door that made the
model act on arbitrary external text would be an injection funnel; a member
replies in-thread when they want the agent engaged.

No Idempotency-Key on either: a trigger is a request to run *now* (each call
is a new run, exactly like pressing the manual button twice), and a message
post is an append the caller can see. Both audit.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor
from ..database import get_db
from ..models import Message, Workflow
from ..schemas import ApiModel
from ..services.audit import record_audit
from ..services.conversations import resolve_visible
from ..services.workflows import executor, inputs, parse_graph
from .ratelimit import public_rate_limit, token_rate_limit

router = APIRouter(prefix="/api/hooks", tags=["hooks"])

MAX_MESSAGE_CHARS = 8000


class HookTriggerRequest(BaseModel):
    #: The `{{ input.* }}` namespace for the run — the same contract as the
    #: manual run button's payload.
    payload: Dict[str, Any] = Field(default_factory=dict)


class HookTriggeredOut(ApiModel):
    workflow_run_id: str
    workflow_id: str
    status: str


class HookMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class HookMessageOut(ApiModel):
    id: str
    conversation_id: str
    #: Always "" — no agent turn is behind this message.
    run_id: str
    created_at: datetime


@router.post(
    "/workflows/{workflow_id}/trigger",
    response_model=HookTriggeredOut,
    status_code=202,
    dependencies=[Depends(public_rate_limit("hooks-ip"))],
)
def trigger_workflow(
    workflow_id: str,
    payload: HookTriggerRequest,
    background_tasks: BackgroundTasks,
    actor: Actor = Depends(token_rate_limit("hook-trigger", tier="heavy")),
    db: Session = Depends(get_db),
) -> HookTriggeredOut:
    """Start a run of one of the token workspace's own workflows. 202.

    The mirror of `run_workflow` with `trigger="webhook"`: resolve under the
    token's workspace FIRST (foreign ids uniformly 404), refuse a disabled
    workflow, 422 a payload that does not satisfy the declared inputs while
    the caller is still holding it, then queue the graph on the background
    path every other trigger uses.
    """
    workflow = db.scalar(
        select(Workflow).where(
            Workflow.id == workflow_id,
            Workflow.workspace_id == actor.workspace_id,
        )
    )
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if workflow.status == "disabled":
        raise HTTPException(status_code=409, detail="This workflow is disabled")
    graph, _ = parse_graph(_graph_document(workflow))
    if graph is not None:
        try:
            inputs.bind(graph, payload.payload)
        except inputs.InputBindingError as exc:
            raise HTTPException(
                status_code=422, detail={"inputs": exc.problems}
            ) from exc
    workflow_run = executor.start_run(
        db,
        workflow,
        user_id=actor.user_id,
        trigger="webhook",
        payload=payload.payload,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="workflow.webhook_triggered",
        resource_type="workflow_run",
        resource_id=workflow_run.id,
        detail={"workflow_id": workflow.id},
    )
    db.commit()
    background_tasks.add_task(executor.process_workflow_run, workflow_run.id)
    return HookTriggeredOut(
        workflow_run_id=workflow_run.id,
        workflow_id=workflow.id,
        status=workflow_run.status,
    )


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=HookMessageOut,
    status_code=201,
    dependencies=[Depends(public_rate_limit("hooks-ip"))],
)
def post_message(
    conversation_id: str,
    payload: HookMessageRequest,
    actor: Actor = Depends(token_rate_limit("hook-message", tier="heavy")),
    db: Session = Depends(get_db),
) -> HookMessageOut:
    """Append an external note to a thread the token's member can see.

    Visibility goes through `conversations.resolve_visible` for the minting
    member — a foreign workspace's thread and a colleague's personal one are
    the same 404. The message is a user-role row with `run_id=""`, exactly as
    a message cron posts one; nothing is executed on its account.
    """
    conversation = resolve_visible(
        db,
        workspace_id=actor.workspace_id,
        user_id=actor.user_id,
        conversation_id=conversation_id,
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    message = Message(
        workspace_id=actor.workspace_id,
        conversation_id=conversation.id,
        run_id="",
        created_by=actor.user_id,
        role="user",
        content=payload.content,
    )
    db.add(message)
    db.flush()
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="hook.message_posted",
        resource_type="conversation",
        resource_id=conversation.id,
        detail={"message_id": message.id},
    )
    db.commit()
    return HookMessageOut(
        id=message.id,
        conversation_id=conversation.id,
        run_id="",
        created_at=message.created_at,
    )


def _graph_document(workflow: Workflow) -> Dict[str, Any]:
    try:
        parsed = json.loads(workflow.graph_json or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
