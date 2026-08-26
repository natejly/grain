from __future__ import annotations

import json
from typing import Any, List, Literal, Optional, Type, TypeVar, cast

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..clock import utcnow
from ..database import get_db
from ..models import (
    SHARED_OWNER,
    AgentToolCall,
    Conversation,
    Membership,
    Run,
    Tool,
    ToolCall,
    ToolPolicy,
    WorkflowRun,
)
from ..schemas import (
    AgentApprovalRequest,
    AgentToolCallOut,
    ApprovalRequest,
    ToolArtifact,
    ToolCallOut,
    ToolInfoOut,
    ToolPolicyOut,
    ToolPolicyRequest,
)
from ..services import conversations
from ..services.agent_loop import PLAN, policy_scope_for_run
from ..services.artifacts import documents, proposals
from ..services.audit import record_audit
from ..services.events import append_event
from ..services.llm_tools import (
    ASK_USER,
    EXIT_PLAN_MODE,
    ToolContext,
    registry_families,
)
from ..services.runs import deny_tool_call, execute_tool_call, resume_run
from ..services.subjects import open_document_id
from ..services.workflows import executor as workflow_executor
from ..services.workflows.inputs import InputBindingError
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
        approved_by_mode=call.approved_by_mode,
        assigned_to=call.assigned_to,
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
    assignee_gate: bool = False,
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

    `assignee_gate` folds the assignment check into the same CAS — only
    meaningful for `AgentToolCall`, the one decidable table with `assigned_to`.
    The route's plain `if` on the assignee gives the friendly 409, but an
    assign racing a decide can move `assigned_to` between that read and this
    write; putting the predicate in the WHERE means a decider who was raced by
    an assignment-to-someone-else loses here instead of deciding a call that
    now names another member.
    """
    criteria = [
        model.id == call_id,
        model.workspace_id == workspace_id,
        model.status == "proposed",
    ]
    if assignee_gate:
        criteria.append(AgentToolCall.assigned_to.in_(("", actor_id)))
    # The cast is only about typing: an ORM-enabled UPDATE really does return a
    # CursorResult, but Session.execute is annotated as the generic Result.
    claimed = cast(
        "CursorResult[Any]",
        db.execute(
            update(model)
            .where(*criteria)
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
        # LEFT JOIN so a run with no chat conversation (automation) still lists.
        .outerjoin(Conversation, Conversation.id == Run.conversation_id)
        .where(
            AgentToolCall.workspace_id == actor.workspace_id,
            # Within-workspace gate: a personal thread's parked call must not
            # list for another member. Automation and visible threads still do.
            conversations.run_activity_predicate(actor_user_id=actor.user_id),
        )
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
        # LEFT JOIN so a run with no chat conversation (automation) still lists.
        .outerjoin(Conversation, Conversation.id == Run.conversation_id)
        .where(
            ToolCall.workspace_id == actor.workspace_id,
            # Within-workspace gate: a personal thread's parked call must not
            # list for another member. Automation and visible threads still do.
            conversations.run_activity_predicate(actor_user_id=actor.user_id),
        )
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
    # A member must not decide a call parked on another member's personal thread:
    # its run resolves by workspace_id but its conversation is not visible to
    # them. Automation and shared/own/document threads pass — same gate as list.
    if not conversations.run_activity_visible(
        db,
        actor_workspace_id=actor.workspace_id,
        actor_user_id=actor.user_id,
        run=run,
    ):
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
    owner_id: str = SHARED_OWNER,
) -> ToolPolicy:
    """Set one verdict for one tool, in one scope, for one owner.

    Neither filter is optional now that (workspace_id, owner_id, tool_name,
    scope) is the unique key. Without the scope this would find a workflow grant
    and overwrite it with the answer to a question about chat; without the owner
    it would find the *workspace's* grant and overwrite it with one person's
    answer — silently widening a personal decision into everybody's, which is the
    exact bug ADR 0010 exists to remove.
    """
    row = db.scalar(
        select(ToolPolicy).where(
            ToolPolicy.workspace_id == workspace_id,
            ToolPolicy.owner_id == owner_id,
            ToolPolicy.tool_name == tool_name,
            ToolPolicy.scope == scope,
        )
    )
    if row is None:
        row = ToolPolicy(
            workspace_id=workspace_id,
            owner_id=owner_id,
            tool_name=tool_name,
            scope=scope,
            created_by=actor_id,
        )
        db.add(row)
    row.policy = policy
    return row


@router.get("/tools", response_model=List[ToolInfoOut])
def list_registry_tools(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[ToolInfoOut]:
    """The live tool registry, grouped by family — the provisioning checklist.

    Built from the same `registry_families` the loop's `build_registry`
    flattens, so what an agent editor offers and what a turn can call cannot
    disagree. Like every registry build, this consults the workspace's MCP
    servers, so it reflects what is connected *now*.
    """
    context = ToolContext(
        workspace_id=actor.workspace_id,
        user_id=actor.user_id,
        conversation_id="",
    )
    out: List[ToolInfoOut] = []
    for family, tools in registry_families(db, context):
        for name in sorted(tools):
            spec = tools[name]
            out.append(
                ToolInfoOut(
                    name=spec.name,
                    description=spec.description,
                    read_only=spec.read_only,
                    family=family,
                )
            )
    return out


def _policy_out(row: ToolPolicy) -> ToolPolicyOut:
    return ToolPolicyOut(
        tool_name=row.tool_name,
        policy=row.policy,
        scope=row.scope,
        shared=row.owner_id == SHARED_OWNER,
        created_at=row.created_at,
        updated_at=row.updated_at,
        created_by=row.created_by,
    )


@router.get("/tool-policies", response_model=List[ToolPolicyOut])
def list_tool_policies(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[ToolPolicyOut]:
    """Every standing verdict that applies to the caller, in both scopes.

    The workspace's own, plus this member's — which is exactly the set
    `evaluate_policy` consults for them, so what a person can review here is what
    can actually act on their behalf. Another member's grants are absent for the
    same reason they are ignored: they are not authority anyone holds over you.

    This is the only way a grant becomes visible again. "Always allow" is one
    click on an approval card and it removes the approval park permanently, so a
    surface that lists what has been granted — and the DELETE below that takes it
    back — is what keeps that click reversible.
    """
    rows = db.scalars(
        select(ToolPolicy)
        .where(
            ToolPolicy.workspace_id == actor.workspace_id,
            ToolPolicy.owner_id.in_({SHARED_OWNER, actor.user_id}),
        )
        # Scope and tier break the tie: three rows can now name one tool, and a
        # list whose order changes between reads is a list a UI cannot diff.
        .order_by(
            ToolPolicy.tool_name.asc(),
            ToolPolicy.scope.asc(),
            ToolPolicy.owner_id.asc(),
        )
    )
    return [_policy_out(row) for row in rows]


@router.put("/tool-policies", response_model=ToolPolicyOut)
def set_tool_policy(
    payload: ToolPolicyRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> ToolPolicyOut:
    """Record a standing verdict, for the caller or for the whole workspace.

    `shared` defaults to False, so the ordinary grant is personal. Writing one on
    everybody's behalf is standing write authority over people who were not asked,
    which is an owner's decision — the same gate spend limits and member
    management already sit behind. A member asking for it is refused rather than
    quietly downgraded to a personal grant: they would be told the workspace was
    covered when it was not, which is worse than a 403.
    """
    if payload.shared and actor.role != "owner":
        raise HTTPException(
            status_code=403,
            detail="Only a workspace owner can set a policy for every member",
        )
    owner_id = SHARED_OWNER if payload.shared else actor.user_id
    row = _upsert_policy(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        tool_name=payload.tool_name,
        policy=payload.policy,
        scope=payload.scope,
        owner_id=owner_id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="tool_policy.set",
        resource_type="tool_policy",
        resource_id=payload.tool_name,
        detail={
            "policy": payload.policy,
            "scope": payload.scope,
            "shared": payload.shared,
        },
    )
    db.commit()
    return _policy_out(row)


@router.delete("/tool-policies/{tool_name}", status_code=204)
def revoke_tool_policy(
    tool_name: str,
    scope: Literal["chat", "workflow"] = Query(...),
    shared: bool = Query(False),
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

    `shared` is a query flag rather than a second route, and it defaults to False
    for the same reason the setter's does: you take back your own grant unless
    you say otherwise, and taking back the workspace's is an owner's act.
    """
    if shared and actor.role != "owner":
        raise HTTPException(
            status_code=403,
            detail="Only a workspace owner can revoke a policy for every member",
        )
    owner_id = SHARED_OWNER if shared else actor.user_id
    row = db.scalar(
        select(ToolPolicy).where(
            ToolPolicy.workspace_id == actor.workspace_id,
            ToolPolicy.owner_id == owner_id,
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
        detail={"policy": previous, "scope": scope, "shared": shared},
    )
    db.commit()


def _hunk_amendment(
    db: Session,
    *,
    call: AgentToolCall,
    run: Run,
    payload: AgentApprovalRequest,
    actor: Actor,
) -> Optional[dict[str, Any]]:
    """Validate a partial approval, and turn it into the executor's amendment.

    Checked here rather than at execution time because this is the request the
    user is waiting on: a selection made against a diff the document has since
    outgrown must fail in front of them, not three seconds later inside a
    background task where the only trace is a tool error the model paraphrases.
    """
    if payload.accepted_hunks is None:
        return None
    if payload.decision != "approved":
        raise HTTPException(
            status_code=422,
            detail="accepted_hunks only applies to an approval",
        )
    if call.name != "edit_document":
        raise HTTPException(
            status_code=422,
            detail=f"“{call.name}” cannot be approved one hunk at a time",
        )
    args = proposals.arguments_of(call.arguments_json)
    document = proposals.target_document(
        db,
        workspace_id=actor.workspace_id,
        name=call.name,
        args=args,
        open_document_id=open_document_id(db, run),
    )
    segments = proposals.review_segments(document, args)
    try:
        documents.select_hunks(payload.accepted_hunks, documents.hunk_count(segments))
    except documents.DocumentError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"accepted_hunks": sorted(set(payload.accepted_hunks))}


def _validate_manual_submission(
    db: Session,
    *,
    call: AgentToolCall,
    run: Run,
    payload: AgentApprovalRequest,
    actor: Actor,
) -> None:
    """Validate the values a person typed at a manual node, 422ing an invalid one.

    The manual twin of `_hunk_amendment`: the person is waiting on this request,
    so values that violate the node's declared `fields` must fail in front of
    them, not in the background task that resumes the run. Only a manual node's
    parked call reads `inputs` — an ordinary tool approval ignores the field — and
    only an approval carries them; a rejection cancels the run regardless.
    """
    if call.name != workflow_executor.MANUAL_TOOL_NAME:
        return
    if payload.decision != "approved":
        return
    workflow_run = db.scalar(
        select(WorkflowRun).where(
            WorkflowRun.run_id == run.id,
            WorkflowRun.workspace_id == actor.workspace_id,
        )
    )
    if workflow_run is None:
        return
    try:
        workflow_executor.check_manual_inputs(
            db, workflow_run, tool_call_id=call.id, submitted=payload.inputs
        )
    except InputBindingError as exc:
        raise HTTPException(status_code=422, detail="; ".join(exc.problems)) from exc


class AgentCallAssignRequest(BaseModel):
    """Who a parked approval should wait on. "" hands it back to anyone."""

    user_id: str = ""


@router.post(
    "/agent-tool-calls/{tool_call_id}/assign", response_model=AgentToolCallOut
)
def assign_agent_tool_call(
    tool_call_id: str,
    payload: AgentCallAssignRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> AgentToolCallOut:
    """Route a parked approval to one member, or back to anyone with "".

    Routing, not deciding: the call stays `proposed`, the run stays parked, and
    the compare-and-set decision claim is untouched. Setting the same assignee
    twice is the same state twice — a natural upsert — which is why this takes
    no Idempotency-Key. Foreign tool-call ids and foreign user ids both answer
    404: the refusal must confirm neither the call nor the user exists.
    """
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
    # Same gate as the decision endpoint: a member must not route (or even
    # learn about) a call parked on another member's personal thread.
    if not conversations.run_activity_visible(
        db,
        actor_workspace_id=actor.workspace_id,
        actor_user_id=actor.user_id,
        run=run,
    ):
        raise HTTPException(status_code=404, detail="Tool call not found")
    if call.status != "proposed" or run.status != "waiting_for_approval":
        raise HTTPException(status_code=409, detail="Tool call already decided")
    if payload.user_id:
        member = db.scalar(
            select(Membership).where(
                Membership.workspace_id == actor.workspace_id,
                Membership.user_id == payload.user_id,
            )
        )
        if member is None:
            # 404, indistinguishable from a user that does not exist — the
            # refusal must not confirm a foreign workspace's user id.
            raise HTTPException(status_code=404, detail="Member not found")
        # The assignee must pass the same run-visibility gate the assigner did:
        # routing a private thread's park to a member who cannot see the run
        # would create an approval only the assigner can act on — the assignee's
        # inbox never lists it, their decide 404s, and everyone else 409s. A
        # 409 rather than 404: the member exists and the assigner can already
        # see the whole run, so nothing is disclosed by saying why.
        if not conversations.run_activity_visible(
            db,
            actor_workspace_id=actor.workspace_id,
            actor_user_id=payload.user_id,
            run=run,
        ):
            raise HTTPException(
                status_code=409,
                detail="That member cannot view this thread",
            )
    # No Notification row for the assignee, deliberately: the assignment
    # already surfaces through their approvals badge (`waitingOnMe` counts
    # rows assigned to them), so a notification would double-count the same
    # actionable item in the same Inbox.
    #
    # The write is a compare-and-set on `status = 'proposed'`, not the plain
    # attribute write the 409 above pre-checked: assign racing decide would
    # otherwise re-park an already-decided row's `assigned_to`. Same shape as
    # `_claim_decision`, settled by the database while the row is held.
    claimed = cast(
        "CursorResult[Any]",
        db.execute(
            update(AgentToolCall)
            .where(
                AgentToolCall.id == call.id,
                AgentToolCall.workspace_id == actor.workspace_id,
                AgentToolCall.status == "proposed",
            )
            .values(assigned_to=payload.user_id)
        ),
    ).rowcount
    if not claimed:
        raise HTTPException(status_code=409, detail="Tool call already decided")
    append_event(
        db,
        workspace_id=actor.workspace_id,
        run_id=run.id,
        event_type="tool.assigned",
        payload={"tool_call_id": call.id, "assigned_to": payload.user_id},
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="tool_call.assigned",
        resource_type="agent_tool_call",
        resource_id=call.id,
        detail={"tool": call.name, "assigned_to": payload.user_id},
    )
    db.commit()
    return _agent_tool_call_out(call, run.conversation_id)


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
    # A member must not decide a call parked on another member's personal thread:
    # its run resolves by workspace_id but its conversation is not visible to
    # them. Automation and shared/own/document threads pass — same gate as list.
    if not conversations.run_activity_visible(
        db,
        actor_workspace_id=actor.workspace_id,
        actor_user_id=actor.user_id,
        run=run,
    ):
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
    # Assignment is routing: while the row names a member, only that member (or
    # the unassigned '') may answer. A 409 like every other state conflict —
    # and only after the 404s above, so a foreign-id probe learns nothing new.
    if call.assigned_to not in ("", actor.user_id):
        raise HTTPException(
            status_code=409,
            detail="This approval is assigned to another member",
        )
    amendment = _hunk_amendment(db, call=call, run=run, payload=payload, actor=actor)
    _validate_manual_submission(db, call=call, run=run, payload=payload, actor=actor)
    if call.name == ASK_USER and payload.decision == "approved" and payload.inputs:
        # The typed answer rides the same channel a reviewer's accepted hunks
        # do: merged into the arguments on the way to the executor, never
        # written over `arguments_json` — the model's question stays the record
        # of what was asked, the audit row records that it was answered.
        answer = str(payload.inputs.get("answer") or "").strip()
        if answer:
            amendment = {"answer": answer[:4000]}

    if not _claim_decision(
        db,
        AgentToolCall,
        call_id=call.id,
        workspace_id=actor.workspace_id,
        decision="approved" if payload.decision == "approved" else "denied",
        actor_id=actor.user_id,
        assignee_gate=True,
    ):
        # Losing here is the whole point: the reviewer who was raced must not
        # also schedule `resume_run`, or the tool the other reviewer denied is
        # executed by the loser's background task.
        raise HTTPException(status_code=409, detail="Tool call already decided")
    if call.name == EXIT_PLAN_MODE and payload.decision == "approved":
        # Approving the plan IS leaving plan mode, and it happens HERE — before
        # the resume is scheduled — so `_continue` re-enters the loop under the
        # restored mode: full registry, no plan instructions, and the rest of
        # this same turn can implement what was just approved. (The executor of
        # the parked call writes nothing; a denial changes nothing, so the model
        # revises the plan still inside plan mode.)
        #
        # Restores the approver's OWN default — the same seed a new thread of
        # theirs gets — rather than whatever mode preceded plan. Two things had
        # to stay true and this is what keeps both: nothing re-arms a posture
        # nobody re-asked for, and a member who never chose to be asked does not
        # find themselves being asked because they once used plan mode. The
        # person approving the plan is the one whose default is read, since
        # approving is the act that chose to leave.
        conversation = db.scalar(
            select(Conversation).where(
                Conversation.id == run.conversation_id,
                Conversation.workspace_id == actor.workspace_id,
            )
        )
        if conversation is not None and conversation.approval_mode == PLAN:
            restored = conversations.default_approval_mode(
                db, workspace_id=actor.workspace_id, user_id=actor.user_id
            )
            conversation.approval_mode = restored
            record_audit(
                db,
                workspace_id=actor.workspace_id,
                actor_id=actor.user_id,
                action="conversation.approval_mode_set",
                resource_type="conversation",
                resource_id=conversation.id,
                detail={"from": PLAN, "to": restored, "via": EXIT_PLAN_MODE},
            )
    if payload.remember and call.name not in (
        workflow_executor.MANUAL_TOOL_NAME,
        # `exit_plan_mode` always parks by construction (`evaluate_policy`'s
        # plan branch never consults standing rows for it), so a remembered
        # allow would gate nothing and only litter the policy list — the same
        # reasoning as the manual sentinel below.
        EXIT_PLAN_MODE,
        # `ask_user` parks by construction too (`force_ask` clamps every allow
        # back to ask): a standing allow could never pre-answer a question
        # addressed to a person, so remembering one would gate nothing.
        ASK_USER,
    ):
        # "Always allow" answers the question the card asked, and no other. A
        # card raised by a workflow node is asking about unattended execution, so
        # remembering it grants at workflow scope; a chat card grants at chat
        # scope. Recording both against one workspace-wide row is the standing
        # grant ADR 0007 named as its sharpest residual risk.
        #
        # And it is granted to the person who clicked, never to the workspace.
        # This is the change ADR 0010 exists for: one member accepting the
        # consequence of a standing `send_email` allow used to accept it on
        # everyone's behalf and remove their approval park too — which is the one
        # containment prompt injection has to get past. Granting for every member
        # is still possible and is now a deliberate owner action at
        # `PUT /api/tool-policies` with `shared: true`.
        #
        # A manual node never consults resolve_policy — it always pauses — so a
        # standing grant on the `__manual__` sentinel would gate nothing and only
        # litter the workspace policy list with a tool that does not exist.
        _upsert_policy(
            db,
            workspace_id=actor.workspace_id,
            actor_id=actor.user_id,
            tool_name=call.name,
            policy="allow" if payload.decision == "approved" else "deny",
            scope=policy_scope_for_run(db, run),
            owner_id=actor.user_id,
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
        detail={
            "tool": call.name,
            "remember": payload.remember,
            # The audit row, not `arguments_json`, is where a partial approval
            # is recorded. The arguments column stays the model's request; this
            # is the human's answer to it, and the two must not be confused.
            **(
                {"accepted_hunks": payload.accepted_hunks}
                if amendment and payload.accepted_hunks
                else {}
            ),
            **({"answered": True} if amendment and "answer" in amendment else {}),
        },
    )
    db.commit()
    # Denied calls resume too: the loop feeds the denial back as the tool's
    # output so the model can answer around it instead of the run dying.
    background_tasks.add_task(
        resume_run, run.id, call.id, payload.decision, amendment, payload.inputs
    )
    return _agent_tool_call_out(call, run.conversation_id)

