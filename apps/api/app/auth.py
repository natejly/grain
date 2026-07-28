from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .database import get_db
from .models import Agent, Membership, Tool, ToolGrant, User, Workspace

DEFAULT_WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"
DEFAULT_USER_ID = "00000000-0000-4000-8000-000000000002"
DEFAULT_AGENT_ID = "00000000-0000-4000-8000-000000000003"
DEFAULT_TOOL_ID = "00000000-0000-4000-8000-000000000004"


@dataclass(frozen=True)
class Actor:
    user_id: str
    user_name: str
    workspace_id: str
    workspace_name: str
    role: str


def ensure_development_identity(db: Session) -> None:
    workspace = db.get(Workspace, DEFAULT_WORKSPACE_ID)
    if workspace is None:
        db.add(Workspace(id=DEFAULT_WORKSPACE_ID, name="Acme Knowledge Lab"))
    user = db.get(User, DEFAULT_USER_ID)
    if user is None:
        db.add(User(id=DEFAULT_USER_ID, email="demo@example.com", name="Nate"))
    membership = db.scalar(
        select(Membership).where(
            Membership.workspace_id == DEFAULT_WORKSPACE_ID,
            Membership.user_id == DEFAULT_USER_ID,
        )
    )
    if membership is None:
        db.add(
            Membership(
                workspace_id=DEFAULT_WORKSPACE_ID,
                user_id=DEFAULT_USER_ID,
                role="owner",
            )
        )
    agent = db.get(Agent, DEFAULT_AGENT_ID)
    if agent is None:
        db.add(
            Agent(
                id=DEFAULT_AGENT_ID,
                workspace_id=DEFAULT_WORKSPACE_ID,
                name="Research partner",
                instructions="Answer from workspace evidence and request approval before tools.",
            )
        )
    tool = db.get(Tool, DEFAULT_TOOL_ID)
    if tool is None:
        db.add(
            Tool(
                id=DEFAULT_TOOL_ID,
                workspace_id=DEFAULT_WORKSPACE_ID,
                name="github-zen",
                description="Fetch the public GitHub Zen message",
                base_url="https://api.github.com/zen",
                requires_approval=True,
            )
        )
    grant = db.scalar(
        select(ToolGrant).where(
            ToolGrant.agent_id == DEFAULT_AGENT_ID,
            ToolGrant.tool_id == DEFAULT_TOOL_ID,
        )
    )
    if grant is None:
        db.add(
            ToolGrant(
                workspace_id=DEFAULT_WORKSPACE_ID,
                agent_id=DEFAULT_AGENT_ID,
                tool_id=DEFAULT_TOOL_ID,
            )
        )
    db.commit()


def get_actor(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    x_user_id: str = Header(default=DEFAULT_USER_ID),
    x_workspace_id: str = Header(default=DEFAULT_WORKSPACE_ID),
) -> Actor:
    if settings.app_env != "development":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Production authentication adapter is not configured",
        )
    membership = db.scalar(
        select(Membership).where(
            Membership.workspace_id == x_workspace_id,
            Membership.user_id == x_user_id,
        )
    )
    if membership is None:
        raise HTTPException(status_code=403, detail="Workspace access denied")
    user = db.get(User, x_user_id)
    workspace = db.get(Workspace, x_workspace_id)
    if user is None or workspace is None:
        raise HTTPException(status_code=403, detail="Workspace identity is invalid")
    return Actor(
        user_id=user.id,
        user_name=user.name,
        workspace_id=workspace.id,
        workspace_name=workspace.name,
        role=membership.role,
    )


def require_owner(actor: Actor = Depends(get_actor)) -> Actor:
    if actor.role != "owner":
        raise HTTPException(status_code=403, detail="Owner role required")
    return actor
