from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..auth import DEFAULT_AGENT_ID, Actor, get_actor
from ..config import Settings, get_settings
from ..database import get_db
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
) -> BootstrapResponse:
    return BootstrapResponse(
        identity=Identity(
            user_id=actor.user_id,
            user_name=actor.user_name,
            workspace_id=actor.workspace_id,
            workspace_name=actor.workspace_name,
            role=actor.role,
        ),
        default_agent_id=DEFAULT_AGENT_ID,
        model_provider=ModelProviderStatus(
            provider=settings.active_model_provider,
            configured=(
                settings.has_openai_key
                if settings.active_model_provider == "openai"
                else True
            ),
            model=(
                settings.openai_model
                if settings.active_model_provider == "openai"
                else "deterministic-local"
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
