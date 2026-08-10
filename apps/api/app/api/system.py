from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..config import Settings, get_settings
from ..database import get_db
from ..models import Agent
from ..schemas import BootstrapResponse, HealthResponse, Identity, ModelProviderStatus

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
            # an openai process with no key, so this is False only for the
            # scripted test double.
            configured=settings.active_model_provider == "openai",
            model=(
                settings.openai_model
                if settings.active_model_provider == "openai"
                else "scripted-double"
            ),
        ),
        feature_flags={
            "cited_memory": True,
            "read_only_tool": True,
            "graph_memory": True,
            "dashboards": True,
            "generated_apps": True,
        },
    )
