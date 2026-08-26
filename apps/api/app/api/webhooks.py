"""Managing outbound webhook endpoints — where workspace events may be sent.

Owner-gated end to end, like API tokens and the spend ceiling: standing
egress of workspace activity to an external URL is an organizational
decision, not a per-member convenience. The signing secret is write-only —
stored Fernet-encrypted, surfaced as `has_secret` (the mcp.tsx convention),
never echoed. URLs are validated at create AND update with the same SSRF
checks every delivery re-runs at send time (`services/webhooks` documents the
allowlist policy); a URL that resolves to this deployment's own network is a
422 while the owner is still holding the form.

`PUT` takes no Idempotency-Key — it is a natural upsert of the row's own
fields (the F4 precedent); `POST` takes one, because each create mints a row.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, require_owner
from ..config import get_settings
from ..database import get_db
from ..models import WebhookDelivery, WebhookEndpoint
from ..schemas import ApiModel
from ..services import webhooks as webhook_service
from ..services.audit import record_audit
from ..services.crypto import EncryptionNotConfiguredError, encrypt_secret
from ..services.tools import ToolSecurityError, validate_public_https_url
from .dependencies import idempotency_key
from .idempotency import find_replay, record_key, replayed_resource_gone

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

#: The UI's recent-deliveries panel is a glance, not an export.
MAX_DELIVERIES = 50


class WebhookEndpointOut(ApiModel):
    id: str
    name: str
    url: str
    events: List[str]
    enabled: bool
    #: Whether a signing secret is stored. The secret itself never leaves.
    has_secret: bool
    created_by: str
    created_at: datetime


class WebhookDeliveryOut(ApiModel):
    id: str
    endpoint_id: str
    event: str
    status: str
    attempts: int
    last_error: str
    created_at: datetime
    sent_at: Optional[datetime]


class WebhookCreateRequest(BaseModel):
    name: str = Field(default="", max_length=120)
    url: str = Field(min_length=1, max_length=600)
    events: List[str] = Field(default_factory=list)
    #: Optional HMAC signing secret; write-only, encrypted at rest.
    secret: str = Field(default="", max_length=200)


class WebhookUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    url: Optional[str] = Field(default=None, min_length=1, max_length=600)
    events: Optional[List[str]] = None
    enabled: Optional[bool] = None


def _out(endpoint: WebhookEndpoint) -> WebhookEndpointOut:
    try:
        events = [
            item
            for item in json.loads(endpoint.events_json or "[]")
            if isinstance(item, str)
        ]
    except ValueError:
        events = []
    return WebhookEndpointOut(
        id=endpoint.id,
        name=endpoint.name,
        url=endpoint.url,
        events=events,
        enabled=endpoint.enabled,
        has_secret=bool(endpoint.secret_encrypted),
        created_by=endpoint.created_by,
        created_at=endpoint.created_at,
    )


def _checked_events(events: List[str]) -> str:
    unknown = [event for event in events if event not in webhook_service.EVENTS]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown events: {', '.join(sorted(set(unknown)))}",
        )
    # De-duplicated, in vocabulary order — a stable answer whatever the form sent.
    return json.dumps([event for event in webhook_service.EVENTS if event in events])


def _checked_url(url: str) -> str:
    try:
        validate_public_https_url(
            url, get_settings(), require_allowlist=False
        )
    except ToolSecurityError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return url


def _load(db: Session, actor: Actor, endpoint_id: str) -> WebhookEndpoint:
    endpoint = db.scalar(
        select(WebhookEndpoint).where(
            WebhookEndpoint.id == endpoint_id,
            WebhookEndpoint.workspace_id == actor.workspace_id,
        )
    )
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return endpoint


@router.post("", response_model=WebhookEndpointOut, status_code=201)
def create_webhook(
    payload: WebhookCreateRequest,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
) -> WebhookEndpointOut:
    replay = find_replay(
        db, workspace_id=actor.workspace_id, operation="webhook.create", key=key
    )
    if replay:
        endpoint = db.scalar(
            select(WebhookEndpoint).where(
                WebhookEndpoint.id == replay.resource_id,
                WebhookEndpoint.workspace_id == actor.workspace_id,
            )
        )
        if endpoint is None:
            raise replayed_resource_gone()
        return _out(endpoint)
    events_json = _checked_events(payload.events)
    url = _checked_url(payload.url)
    secret_encrypted = ""
    if payload.secret:
        try:
            secret_encrypted = encrypt_secret(payload.secret)
        except EncryptionNotConfiguredError as exc:
            raise HTTPException(
                status_code=503,
                detail="Secret storage is not configured on this deployment",
            ) from exc
    endpoint = WebhookEndpoint(
        workspace_id=actor.workspace_id,
        name=payload.name.strip()[:120],
        url=url,
        events_json=events_json,
        secret_encrypted=secret_encrypted,
        enabled=True,
        created_by=actor.user_id,
    )
    db.add(endpoint)
    db.flush()
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="webhook.create",
        key=key,
        resource_id=endpoint.id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="webhook.created",
        resource_type="webhook_endpoint",
        resource_id=endpoint.id,
        detail={"url": endpoint.url, "events": payload.events},
    )
    db.commit()
    return _out(endpoint)


@router.get("", response_model=List[WebhookEndpointOut])
def list_webhooks(
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
) -> List[WebhookEndpointOut]:
    rows = db.scalars(
        select(WebhookEndpoint)
        .where(WebhookEndpoint.workspace_id == actor.workspace_id)
        .order_by(WebhookEndpoint.created_at, WebhookEndpoint.id)
    )
    return [_out(row) for row in rows]


@router.get("/deliveries", response_model=List[WebhookDeliveryOut])
def list_deliveries(
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
) -> List[WebhookDeliveryOut]:
    """The recent delivery trail, newest first — status chips, not payloads.

    Payload bodies are deliberately not surfaced: they are already summaries
    (ids and titles), but the list exists to answer "is my endpoint healthy",
    and `event` + `status` + `last_error` answer it.
    """
    rows = db.scalars(
        select(WebhookDelivery)
        .where(WebhookDelivery.workspace_id == actor.workspace_id)
        .order_by(WebhookDelivery.created_at.desc(), WebhookDelivery.id)
        .limit(MAX_DELIVERIES)
    )
    return [
        WebhookDeliveryOut(
            id=row.id,
            endpoint_id=row.endpoint_id,
            event=row.event,
            status=row.status,
            attempts=row.attempts,
            last_error=row.last_error,
            created_at=row.created_at,
            sent_at=row.sent_at,
        )
        for row in rows
    ]


@router.put("/{endpoint_id}", response_model=WebhookEndpointOut)
def update_webhook(
    endpoint_id: str,
    payload: WebhookUpdateRequest,
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
) -> WebhookEndpointOut:
    endpoint = _load(db, actor, endpoint_id)
    changed: dict[str, object] = {}
    if payload.name is not None:
        endpoint.name = payload.name.strip()[:120]
        changed["name"] = endpoint.name
    if payload.url is not None:
        endpoint.url = _checked_url(payload.url)
        changed["url"] = endpoint.url
    if payload.events is not None:
        endpoint.events_json = _checked_events(payload.events)
        changed["events"] = payload.events
    if payload.enabled is not None:
        endpoint.enabled = payload.enabled
        changed["enabled"] = payload.enabled
    if changed:
        record_audit(
            db,
            workspace_id=actor.workspace_id,
            actor_id=actor.user_id,
            action="webhook.updated",
            resource_type="webhook_endpoint",
            resource_id=endpoint.id,
            detail=changed,
        )
        db.commit()
    return _out(endpoint)


@router.delete("/{endpoint_id}", status_code=204)
def delete_webhook(
    endpoint_id: str,
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
) -> None:
    """Remove the endpoint. Its delivery history stays — the trail outlives
    the destination, and any still-pending rows become skips at send time."""
    endpoint = _load(db, actor, endpoint_id)
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="webhook.deleted",
        resource_type="webhook_endpoint",
        resource_id=endpoint.id,
        detail={"url": endpoint.url},
    )
    db.delete(endpoint)
    db.commit()
