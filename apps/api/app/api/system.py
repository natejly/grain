from __future__ import annotations

from typing import get_args

from fastapi import APIRouter, Depends
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..config import ReasoningEffort, Settings, get_settings
from ..database import get_db
from ..models import Agent, Membership
from ..schemas import (
    BootstrapResponse,
    DigestStatus,
    HealthResponse,
    Identity,
    ModelProviderStatus,
    ScreenStatus,
)
from ..services import orgs

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse)
def health(db: Session = Depends(get_db)) -> HealthResponse:
    db.execute(text("SELECT 1"))
    return HealthResponse(status="ok", database="ok", version="0.1.0")


@router.get("/api/bootstrap", response_model=BootstrapResponse)
def bootstrap(
    actor: Actor = Depends(get_actor),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> BootstrapResponse:
    # Every workspace now owns its agent — signup creates one — so this can no
    # longer be a constant. Handing out a fixed id would point a new tenant's
    # chat at another tenant's agent.
    default_agent_id = db.scalar(
        select(Agent.id)
        .where(Agent.workspace_id == actor.workspace_id, Agent.enabled.is_(True))
        .order_by(Agent.created_at, Agent.id)
        .limit(1)
    )
    # The caller's own membership row carries their digest preference; read it
    # here so the settings menu needs no second request. A session that has
    # outlived its membership reads as the defaults rather than a 500.
    membership = db.scalar(
        select(Membership).where(
            Membership.workspace_id == actor.workspace_id,
            Membership.user_id == actor.user_id,
        )
    )
    return BootstrapResponse(
        identity=Identity(
            user_id=actor.user_id,
            user_name=actor.user_name,
            workspace_id=actor.workspace_id,
            workspace_name=actor.workspace_name,
            role=actor.role,
        ),
        default_agent_id=default_agent_id or "",
        model_provider=ModelProviderStatus(
            provider=settings.active_model_provider,
            # True whenever a real provider is answering. Startup already refuses
            # a real provider with no key, so this is False only for the
            # scripted test double.
            configured=settings.active_model_provider != "scripted",
            model=(
                settings.default_model
                if settings.active_model_provider != "scripted"
                else "scripted-double"
            ),
            # The scripted double talks to no provider, so its only selectable
            # "model" is itself — the composer must not offer a real allow-list a
            # test deployment cannot honour.
            #
            # Under a real provider the list is the deployment's, narrowed by the
            # organization. Read from the same `orgs.allowed_models` the send
            # route refuses against, so the dropdown and the 422 cannot disagree:
            # an org that excludes a model makes it disappear from the composer
            # rather than making it a choice that fails on click.
            selectable_models=(
                orgs.allowed_models(
                    db, workspace_id=actor.workspace_id, settings=settings
                )
                if settings.active_model_provider != "scripted"
                else ["scripted-double"]
            ),
            # One ladder for both real providers: OpenAI reasoning effort, and
            # Anthropic's own `output_config.effort` — the same five strings —
            # via the harness's `_thinking_kwargs` mapping ("none" disables
            # thinking). Every choice in the dropdown does something on both.
            reasoning_efforts=list(get_args(ReasoningEffort)),
            default_effort=settings.openai_reasoning_effort,
        ),
        screen=ScreenStatus(
            enabled=settings.screen_enabled,
            mode=settings.screen_mode,
            backend=settings.screen_backend,
        ),
        digest=DigestStatus(
            enabled=bool(membership.digest_enabled) if membership else False,
            hour_utc=membership.digest_hour_utc if membership else 9,
        ),
        unrestricted_agent=settings.dev_unrestricted_agent,
        feature_flags={
            "cited_memory": True,
            "read_only_tool": True,
            "graph_memory": True,
            "dashboards": True,
            "generated_apps": True,
        },
    )
