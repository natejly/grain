from __future__ import annotations

from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..clock import utcnow
from ..database import get_db
from ..models import AgentToolCall, IdempotencyRecord, Run, Tool, ToolCall, ToolPolicy
from ..schemas import (
    AgentApprovalRequest,
    AgentToolCallOut,
    ApprovalRequest,
    ToolCallOut,
    ToolPolicyOut,
    ToolPolicyRequest,
)
from ..services.audit import record_audit
from ..services.events import append_event
from ..services.runs import deny_tool_call, execute_tool_call, resume_run
from .dependencies import idempotency_key

router = APIRouter(prefix="/api", tags=["tools"])


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
        created_at=call.created_at,
    )


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
    replay = db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.workspace_id == actor.workspace_id,
            IdempotencyRecord.operation == "tool.decision",
            IdempotencyRecord.key == key,
        )
    )
    if replay:
        return _tool_call_out(call, tool, run.conversation_id)
    if call.status != "proposed":
        raise HTTPException(status_code=409, detail="Tool call already decided")
    if run.status != "waiting_for_approval":
        raise HTTPException(status_code=409, detail="Run is not awaiting this approval")
    call.status = payload.decision
    call.decided_by = actor.user_id
    call.decided_at = utcnow()
    db.add(
        IdempotencyRecord(
            workspace_id=actor.workspace_id,
            operation="tool.decision",
            key=key,
            resource_id=call.id,
        )
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
    db: Session, *, workspace_id: str, actor_id: str, tool_name: str, policy: str
) -> ToolPolicy:
    row = db.scalar(
        select(ToolPolicy).where(
            ToolPolicy.workspace_id == workspace_id,
            ToolPolicy.tool_name == tool_name,
        )
    )
    if row is None:
        row = ToolPolicy(
            workspace_id=workspace_id, tool_name=tool_name, created_by=actor_id
        )
        db.add(row)
    row.policy = policy
    return row


@router.get("/tool-policies", response_model=List[ToolPolicyOut])
def list_tool_policies(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[ToolPolicy]:
    return list(
        db.scalars(
            select(ToolPolicy)
            .where(ToolPolicy.workspace_id == actor.workspace_id)
            .order_by(ToolPolicy.tool_name.asc())
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
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="tool_policy.set",
        resource_type="tool_policy",
        resource_id=payload.tool_name,
        detail={"policy": payload.policy},
    )
    db.commit()
    return row


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
    replay = db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.workspace_id == actor.workspace_id,
            IdempotencyRecord.operation == "agent_tool.decision",
            IdempotencyRecord.key == key,
        )
    )
    if replay:
        return _agent_tool_call_out(call, run.conversation_id)
    if call.status != "proposed":
        raise HTTPException(status_code=409, detail="Tool call already decided")
    if run.status != "waiting_for_approval":
        raise HTTPException(status_code=409, detail="Run is not awaiting this approval")

    call.status = "approved" if payload.decision == "approved" else "denied"
    call.decided_by = actor.user_id
    call.decided_at = utcnow()
    if payload.remember:
        _upsert_policy(
            db,
            workspace_id=actor.workspace_id,
            actor_id=actor.user_id,
            tool_name=call.name,
            policy="allow" if payload.decision == "approved" else "deny",
        )
    db.add(
        IdempotencyRecord(
            workspace_id=actor.workspace_id,
            operation="agent_tool.decision",
            key=key,
            resource_id=call.id,
        )
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

