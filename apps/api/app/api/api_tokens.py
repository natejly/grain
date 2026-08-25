"""Managing the bearer tokens for the workspace's MCP surface.

Owner-gated end to end: handing an external agent standing access to the
workspace is an owner decision, like the spend ceiling and the org bounds.
The secret appears exactly once, in the mint response — after that the
workspace holds only its hash, and the list shows names and stamps.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, require_owner
from ..clock import utcnow
from ..database import get_db
from ..models import ApiToken
from ..schemas import ApiModel
from ..services import api_tokens as token_service
from ..services.audit import record_audit
from .dependencies import idempotency_key
from .idempotency import find_replay, record_key, replayed_resource_gone

router = APIRouter(prefix="/api", tags=["api-tokens"])


class ApiTokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class ApiTokenOut(ApiModel):
    id: str
    name: str
    created_at: datetime
    last_used_at: Optional[datetime]
    revoked_at: Optional[datetime]


class ApiTokenMintedOut(ApiTokenOut):
    #: Present only in the mint response; the server keeps the hash alone.
    secret: str


def _out(token: ApiToken) -> ApiTokenOut:
    return ApiTokenOut(
        id=token.id,
        name=token.name,
        created_at=token.created_at,
        last_used_at=token.last_used_at,
        revoked_at=token.revoked_at,
    )


@router.get("/api-tokens", response_model=List[ApiTokenOut])
def list_api_tokens(
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
) -> List[ApiTokenOut]:
    rows = db.scalars(
        select(ApiToken)
        .where(ApiToken.workspace_id == actor.workspace_id)
        .order_by(ApiToken.created_at, ApiToken.id)
    )
    return [_out(token) for token in rows]


@router.post("/api-tokens", response_model=ApiTokenMintedOut, status_code=201)
def create_api_token(
    payload: ApiTokenCreate,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
) -> ApiTokenMintedOut:
    replay = find_replay(
        db, workspace_id=actor.workspace_id, operation="api_token.create", key=key
    )
    if replay:
        token = db.get(ApiToken, replay.resource_id)
        if token is None or token.workspace_id != actor.workspace_id:
            raise replayed_resource_gone()
        # The secret existed once, in the original response. A replay proves
        # the create happened; it cannot repeat what was never stored.
        return ApiTokenMintedOut(**_out(token).model_dump(), secret="")
    minted = token_service.mint(
        db,
        workspace_id=actor.workspace_id,
        user_id=actor.user_id,
        name=payload.name,
    )
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="api_token.create",
        key=key,
        resource_id=minted.token.id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="api_token.created",
        resource_type="api_token",
        resource_id=minted.token.id,
        detail={"name": minted.token.name},
    )
    db.commit()
    return ApiTokenMintedOut(**_out(minted.token).model_dump(), secret=minted.secret)


@router.delete("/api-tokens/{token_id}", status_code=204)
def revoke_api_token(
    token_id: str,
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
) -> None:
    token = db.get(ApiToken, token_id)
    if token is None or token.workspace_id != actor.workspace_id:
        raise HTTPException(status_code=404, detail="Token not found")
    if token.revoked_at is None:
        token.revoked_at = utcnow()
        record_audit(
            db,
            workspace_id=actor.workspace_id,
            actor_id=actor.user_id,
            action="api_token.revoked",
            resource_type="api_token",
            resource_id=token.id,
            detail={"name": token.name},
        )
        db.commit()
