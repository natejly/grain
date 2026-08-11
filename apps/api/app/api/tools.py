from __future__ import annotations

import json
from typing import Any, List, Literal, Type, TypeVar, cast

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..clock import utcnow
from ..database import get_db
from ..models import AgentToolCall, Run, Tool, ToolCall, ToolPolicy
from ..schemas import (
    AgentApprovalRequest,
    AgentToolCallOut,
    ApprovalRequest,
    ToolArtifact,
    ToolCallOut,
    ToolPolicyOut,
    ToolPolicyRequest,
)
from ..services.agent_loop import policy_scope_for_run
from ..services.audit import record_audit
from ..services.events import append_event
from ..services.runs import deny_tool_call, execute_tool_call, resume_run
from .dependencies import idempotency_key
from .idempotency import find_replay, record_key

router = APIRouter(prefix="/api", tags=["tools"])

#: The two tables that park a call on a human decision. They are separate models
#: (one HTTP tool, one agent function call) but the gate is the same one.
DecidableCall = TypeVar("DecidableCall", ToolCall, AgentToolCall)


def _tool_call_out(call: ToolCall, tool: Tool, conversation_id: str) -> ToolCallOut:
    return ToolCallOut(
        id=call.id,
        run_id=call.run_id,
        conversation_id=conversation_id,
        tool_id=call.tool_id,
        tool_name=tool.name,
        status=call.status,
        request_url=call.request_url,
        response_status=call.response_status,
        response_body=call.response_body,
        error=call.error,
        created_at=call.created_at,
    )


def _artifacts(raw: str) -> List[ToolArtifact]:
    """Descriptors off the row. A row written before the column existed holds
    "", which is not JSON and is also not an error."""
    try:
        return ToolArtifact.collect(json.loads(raw or "[]"))
    except ValueError:
        return []


def _agent_tool_call_out(call: AgentToolCall, conversation_id: str) -> AgentToolCallOut:
    return AgentToolCallOut(
        id=call.id,
        run_id=call.run_id,
        conversation_id=conversation_id,
        name=call.name,
        arguments_json=call.arguments_json,
        proposal_preview=call.proposal_preview,
        status=call.status,
        result_preview=call.result_preview,
        error=call.error,
        latency_ms=call.latency_ms,
        artifacts=_artifacts(call.artifacts_json),
        created_at=call.created_at,
    )


def _claim_decision(
    db: Session,
    model: Type[DecidableCall],
    *,
    call_id: str,
    workspace_id: str,
    decision: str,
    actor_id: str,
) -> bool:
    """Move one *proposed* call to its decision, and say whether we moved it.

    The gate is the `WHERE status = 'proposed'` inside the UPDATE, not an `if`
    in front of it. Two reviewers who both read `proposed` both pass an `if`,
    both write, and both schedule the resume — so a denial is overwritten by an
    approval that a reviewer raced, and the tool the human refused runs anyway.
    The predicate here is evaluated by the database while the row is held for
    writing, so exactly one caller sees rowcount 1 and the loser gets a 409;
    that holds on Postgres row-level concurrency and on SQLite alike, and it
    does not depend on `FOR UPDATE`.

    Returning a bool rather than raising keeps the two routes free to describe
    the conflict in their own words, and keeps the caller honest about the fact
    that losing is an ordinary outcome.
    """
    # The cast is only about typing: an ORM-enabled UPDATE really does return a
    # CursorResult, but Session.execute is annotated as the generic Result.
    claimed = cast(
        "CursorResult[Any]",
        db.execute(
            update(model)
            .where(
                model.id == call_id,
                model.workspace_id == workspace_id,
                model.status == "proposed",
            )
            .values(status=decision, decided_by=actor_id, decided_at=utcnow())
        ),
    ).rowcount
    return bool(claimed)


@router.get("/agent-tool-calls", response_model=List[AgentToolCallOut])
def list_agent_tool_calls(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[AgentToolCallOut]:
    rows = db.execute(
        select(AgentToolCall, Run.conversation_id)
        .join(Run, Run.id == AgentToolCall.run_id)
        .where(AgentToolCall.workspace_id == actor.workspace_id)
        .order_by(AgentToolCall.created_at.desc())
        .limit(50)
    ).all()
    return [_agent_tool_call_out(call, conversation_id) for call, conversation_id in rows]


@router.get("/tool-calls", response_model=List[ToolCallOut])
def list_tool_calls(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[ToolCallOut]:
    rows = db.execute(
        select(ToolCall, Tool, Run.conversation_id)
        .join(Tool, Tool.id == ToolCall.tool_id)
        .join(Run, Run.id == ToolCall.run_id)
        .where(ToolCall.workspace_id == actor.workspace_id)
        .order_by(ToolCall.created_at.desc())
        .limit(50)
    ).all()
    return [
        _tool_call_out(call, tool, conversation_id)
        for call, tool, conversation_id in rows
    ]


@router.post("/tool-calls/{tool_call_id}/decision", response_model=ToolCallOut)
def decide_tool_call(
    tool_call_id: str,
    payload: ApprovalRequest,
    background_tasks: BackgroundTasks,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> ToolCallOut:
    row = db.execute(
        select(ToolCall, Tool)
        .join(Tool, Tool.id == ToolCall.tool_id)
        .where(
            ToolCall.id == tool_call_id,
            ToolCall.workspace_id == actor.workspace_id,
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Tool call not found")
    call, tool = row
    run = db.scalar(
        select(Run).where(
            Run.id == call.run_id,
            Run.workspace_id == actor.workspace_id,
        )
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Tool call not found")
    replay = find_replay(
        db,
        workspace_id=actor.workspace_id,
        operation="tool.decision",
        key=key,
    )
    if replay:
        return _tool_call_out(call, tool, run.conversation_id)
    if run.status != "waiting_for_approval":
        raise HTTPException(status_code=409, detail="Run is not awaiting this approval")
    if not _claim_decision(
        db,
        ToolCall,
        call_id=call.id,
        workspace_id=actor.workspace_id,
        decision=payload.decision,
        actor_id=actor.user_id,
    ):
        raise HTTPException(status_code=409, detail="Tool call already decided")
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="tool.decision",
        key=key,
        resource_id=call.id,
    )
    append_event(
        db,
        workspace_id=actor.workspace_id,
        run_id=run.id,
        event_type="tool." + payload.decision,
        payload={"tool_call_id": call.id, "decision": payload.decision},
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="tool." + payload.decision,
        resource_type="tool_call",
        resource_id=call.id,
        detail={"tool": tool.name, "url": call.request_url},
    )
    db.commit()
    if payload.decision == "approved":
        background_tasks.add_task(execute_tool_call, call.id)
    else:
        background_tasks.add_task(deny_tool_call, call.id)
    return _tool_call_out(call, tool, run.conversation_id)


def _upsert_policy(
    db: Session,
    *,
    workspace_id: str,
    actor_id: str,
    tool_name: str,
    policy: str,
    scope: str = "chat",
) -> ToolPolicy:
    """Set one workspace's verdict for one tool *in one scope*.

    The scope filter is not optional now that (workspace_id, tool_name, scope) is
    the unique key: without it this would happily find a workflow grant and
    overwrite it with the answer to a question about chat.
    """
    row = db.scalar(
        select(ToolPolicy).where(
            ToolPolicy.workspace_id == workspace_id,
            ToolPolicy.tool_name == tool_name,
            ToolPolicy.scope == scope,
        )
    )
    if row is None:
        row = ToolPolicy(
            workspace_id=workspace_id,
            tool_name=tool_name,
            scope=scope,
            created_by=actor_id,
        )
        db.add(row)
    row.policy = policy
    return row


@router.get("/tool-policies", response_model=List[ToolPolicyOut])
def list_tool_policies(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[ToolPolicy]:
    """Every standing verdict this workspace has recorded, in both scopes.

    This is the only way a grant becomes visible again. "Always allow" is one
    click on an approval card and it removes the approval park permanently, so a
    surface that lists what has been granted — and the DELETE below that takes it
    back — is what keeps that click reversible.
    """
    return list(
        db.scalars(
            select(ToolPolicy)
            .where(ToolPolicy.workspace_id == actor.workspace_id)
            # Scope breaks the tie: two rows can name the same tool, and a list
            # whose order changes between reads is a list a UI cannot diff.
            .order_by(ToolPolicy.tool_name.asc(), ToolPolicy.scope.asc())
        )
    )


@router.put("/tool-policies", response_model=ToolPolicyOut)
def set_tool_policy(
    payload: ToolPolicyRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> ToolPolicy:
    row = _upsert_policy(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        tool_name=payload.tool_name,
        policy=payload.policy,
        scope=payload.scope,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="tool_policy.set",
        resource_type="tool_policy",
        resource_id=payload.tool_name,
        detail={"policy": payload.policy, "scope": payload.scope},
    )
    db.commit()
    return row


@router.delete("/tool-policies/{tool_name}", status_code=204)
def revoke_tool_policy(
    tool_name: str,
    scope: Literal["chat", "workflow"] = Query(...),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> None:
    """Delete one standing verdict, restoring the tool's own default.

    Deleting is not the same as PUT-ing `ask`, which is why this exists rather
    than leaving revocation to the setter. A row saying `ask` is still an
    override — it pins a read-only tool to asking forever, and it keeps
    occupying the (workspace, tool, scope) key that `resolve_policy` consults
    first. Removing the row is what actually returns the tool to
    `ToolSpec.read_only`, and it is the only operation that makes the list above
    shrink, which is how a person confirms a grant is gone.

    `scope` is required for the same reason `resolve_policy` refuses to default
    it: the only value a default could take is one of the two, and revoking the
    grant the caller did not mean would leave the other standing while the UI
    reports success. Missing means "say which", not "guess".
    """
    row = db.scalar(
        select(ToolPolicy).where(
            ToolPolicy.workspace_id == actor.workspace_id,
            ToolPolicy.tool_name == tool_name,
            ToolPolicy.scope == scope,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Tool policy not found")
    previous = row.policy
    db.delete(row)
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="tool_policy.revoked",
        resource_type="tool_policy",
        resource_id=tool_name,
        detail={"policy": previous, "scope": scope},
    )
    db.commit()


@router.post(
    "/agent-tool-calls/{tool_call_id}/decision", response_model=AgentToolCallOut
)
def decide_agent_tool_call(
    tool_call_id: str,
    payload: AgentApprovalRequest,
    background_tasks: BackgroundTasks,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> AgentToolCallOut:
    """Approve or deny a tool call the agent loop parked on, then resume the run."""
    call = db.scalar(
        select(AgentToolCall).where(
            AgentToolCall.id == tool_call_id,
            AgentToolCall.workspace_id == actor.workspace_id,
        )
    )
    if call is None:
        raise HTTPException(status_code=404, detail="Tool call not found")
    run = db.scalar(
        select(Run).where(
            Run.id == call.run_id, Run.workspace_id == actor.workspace_id
        )
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Tool call not found")
    replay = find_replay(
        db,
        workspace_id=actor.workspace_id,
        operation="agent_tool.decision",
        key=key,
    )
    if replay:
        return _agent_tool_call_out(call, run.conversation_id)
    if run.status != "waiting_for_approval":
        raise HTTPException(status_code=409, detail="Run is not awaiting this approval")

    if not _claim_decision(
        db,
        AgentToolCall,
        call_id=call.id,
        workspace_id=actor.workspace_id,
        decision="approved" if payload.decision == "approved" else "denied",
        actor_id=actor.user_id,
    ):
        # Losing here is the whole point: the reviewer who was raced must not
        # also schedule `resume_run`, or the tool the other reviewer denied is
        # executed by the loser's background task.
        raise HTTPException(status_code=409, detail="Tool call already decided")
    if payload.remember:
        # "Always allow" answers the question the card asked, and no other. A
        # card raised by a workflow node is asking about unattended execution, so
        # remembering it grants at workflow scope; a chat card grants at chat
        # scope. Recording both against one workspace-wide row is the standing
        # grant ADR 0007 named as its sharpest residual risk.
        _upsert_policy(
            db,
            workspace_id=actor.workspace_id,
            actor_id=actor.user_id,
            tool_name=call.name,
            policy="allow" if payload.decision == "approved" else "deny",
            scope=policy_scope_for_run(db, run),
        )
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="agent_tool.decision",
        key=key,
        resource_id=call.id,
    )
    append_event(
        db,
        workspace_id=actor.workspace_id,
        run_id=run.id,
        event_type="tool." + payload.decision,
        payload={"tool_call_id": call.id, "decision": payload.decision},
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="agent_tool." + payload.decision,
        resource_type="agent_tool_call",
        resource_id=call.id,
        detail={"tool": call.name, "remember": payload.remember},
    )
    db.commit()
    # Denied calls resume too: the loop feeds the denial back as the tool's
    # output so the model can answer around it instead of the run dying.
    background_tasks.add_task(resume_run, run.id, call.id, payload.decision)
    return _agent_tool_call_out(call, run.conversation_id)

