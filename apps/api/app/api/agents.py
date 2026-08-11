"""Agents a person authors: a name, a system prompt, a provisioned tool subset.

The `agents` table predates this module — signup has always written one default
row — but until now nothing read it back. These routes make agents a workspace
resource: list, create, edit, retire. What an agent *does* with its columns
lives in `services/agent_loop.resolve_directives` (instructions + registry
subset per run), not here.

Two invariants this router owns:

- A workspace never drops to zero enabled agents. Chat resolution
  (`api/chat.py`), the bootstrap `default_agent_id`, and the workflow
  executor's backing run all assume one exists; the guard lives here because
  every path that could break the invariant is a route in this file.
- An agent with history is retired, not deleted. `runs.agent_id` is a foreign
  key and past runs must keep resolving; `enabled=False` removes an agent from
  every picker while keeping the record legible. Hard delete works only for an
  agent no run ever used. Workflow graphs naming a deleted agent are caught at
  save/run validation, not scanned here.

v1 deliberately has no per-agent model override; the workspace provider/model
applies to every agent.
"""
from __future__ import annotations

import json
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..database import get_db
from ..models import Agent, Run
from ..schemas import AgentCreate, AgentOut, AgentUpdate
from ..services.audit import record_audit
from .dependencies import idempotency_key
from .idempotency import find_replay, record_key, replayed_resource_gone

router = APIRouter(prefix="/api", tags=["agents"])


def parse_allowed_tools(raw: str) -> Optional[List[str]]:
    """`allowed_tools_json` → the API's Optional[List[str]].

    "" is unset (all tools → None). A stored list — empty included — is an
    explicit grant. Malformed JSON degrades to None rather than failing the
    read: a corrupt allowlist must never make an agent unloadable, and "all
    tools, still policy-gated" is the safe meaning for a value nobody can parse.
    """
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(parsed, list):
        return None
    return [str(item) for item in parsed]


def _serialize_allowed_tools(allowed: Optional[List[str]]) -> str:
    if allowed is None:
        return ""
    return json.dumps(sorted({str(item) for item in allowed}))


def _out(agent: Agent) -> AgentOut:
    return AgentOut(
        id=agent.id,
        name=agent.name,
        description=agent.description,
        instructions=agent.instructions,
        enabled=bool(agent.enabled),
        allowed_tools=parse_allowed_tools(agent.allowed_tools_json),
        created_at=agent.created_at,
        updated_at=agent.updated_at,
    )


def _load(db: Session, actor: Actor, agent_id: str) -> Agent:
    agent = db.scalar(
        select(Agent).where(
            Agent.id == agent_id, Agent.workspace_id == actor.workspace_id
        )
    )
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


def _refuse_if_last_enabled(db: Session, agent: Agent, verb: str) -> None:
    """The zero-enabled-agents guard. Only an *enabled* agent can be the last."""
    if not agent.enabled:
        return
    enabled = db.scalar(
        select(func.count())
        .select_from(Agent)
        .where(Agent.workspace_id == agent.workspace_id, Agent.enabled.is_(True))
    )
    if int(enabled or 0) <= 1:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot {verb} the workspace's only enabled agent — chat and "
                "workflows need one. Create or enable another agent first."
            ),
        )


@router.get("/agents", response_model=List[AgentOut])
def list_agents(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[AgentOut]:
    # Disabled agents included: the editor is where you re-enable one, and a
    # list that hides them would make retirement look like deletion.
    rows = db.scalars(
        select(Agent)
        .where(Agent.workspace_id == actor.workspace_id)
        .order_by(Agent.created_at.asc(), Agent.id.asc())
    )
    return [_out(row) for row in rows]


@router.post("/agents", response_model=AgentOut, status_code=201)
def create_agent(
    payload: AgentCreate,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> AgentOut:
    replay = find_replay(
        db, workspace_id=actor.workspace_id, operation="agent.create", key=key
    )
    if replay:
        agent = db.scalar(
            select(Agent).where(
                Agent.id == replay.resource_id,
                Agent.workspace_id == actor.workspace_id,
            )
        )
        if agent is None:
            raise replayed_resource_gone()
        return _out(agent)

    agent = Agent(
        workspace_id=actor.workspace_id,
        name=payload.name.strip(),
        description=payload.description.strip(),
        instructions=payload.instructions,
        allowed_tools_json=_serialize_allowed_tools(payload.allowed_tools),
        created_by=actor.user_id,
        enabled=True,
    )
    db.add(agent)
    db.flush()  # the id is assigned on insert; the replay and audit rows need it
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="agent.create",
        key=key,
        resource_id=agent.id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="agent.created",
        resource_type="agent",
        resource_id=agent.id,
        detail={
            "name": agent.name,
            "allowed_tools": parse_allowed_tools(agent.allowed_tools_json),
        },
    )
    db.commit()
    return _out(agent)


@router.patch("/agents/{agent_id}", response_model=AgentOut)
def update_agent(
    agent_id: str,
    payload: AgentUpdate,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> AgentOut:
    agent = _load(db, actor, agent_id)
    if payload.enabled is False:
        _refuse_if_last_enabled(db, agent, "disable")

    changed: dict = {}
    if payload.name is not None:
        agent.name = payload.name.strip()
        changed["name"] = agent.name
    if payload.description is not None:
        agent.description = payload.description.strip()
        changed["description"] = True
    if payload.instructions is not None:
        agent.instructions = payload.instructions
        changed["instructions"] = True
    if payload.enabled is not None:
        agent.enabled = payload.enabled
        changed["enabled"] = payload.enabled
    # `clear_allowed_tools` outranks a list: "back to all tools" is an explicit
    # request and must not be shadowed by a stale list in the same payload.
    if payload.clear_allowed_tools:
        agent.allowed_tools_json = ""
        changed["allowed_tools"] = None
    elif payload.allowed_tools is not None:
        agent.allowed_tools_json = _serialize_allowed_tools(payload.allowed_tools)
        changed["allowed_tools"] = parse_allowed_tools(agent.allowed_tools_json)

    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="agent.updated",
        resource_type="agent",
        resource_id=agent.id,
        detail=changed,
    )
    db.commit()
    return _out(agent)


@router.delete("/agents/{agent_id}", status_code=204)
def delete_agent(
    agent_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> None:
    agent = _load(db, actor, agent_id)
    _refuse_if_last_enabled(db, agent, "delete")
    used = db.scalar(select(Run.id).where(Run.agent_id == agent.id).limit(1))
    if used is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "This agent has run history, which must stay attributable — "
                "disable it instead of deleting."
            ),
        )
    name = agent.name
    db.delete(agent)
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="agent.deleted",
        resource_type="agent",
        resource_id=agent_id,
        detail={"name": name},
    )
    db.commit()
