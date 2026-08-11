from __future__ import annotations

import json
import secrets
from datetime import timedelta
from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor, require_owner
from ..clock import utcnow
from ..config import Settings, get_settings
from ..database import get_db
from ..models import IntegrationAccount, OAuthState, SyncJob
from ..schemas import (
    GarminCredentialsIn,
    IntegrationAccountOut,
    IntegrationConnectOut,
    IntegrationProviderOut,
    SyncJobOut,
)
from ..services.audit import record_audit
from ..services.connectors import base as connector_base
from ..services.connectors import garmin as garmin_connector
from ..services.connectors import gmail as gmail_connector
from ..services.connectors.base import ConnectorError
from ..services.connectors.sync_jobs import CONNECTOR_FOR_PROVIDER, run_sync_job
from ..services.crypto import encrypt_secret
from .dependencies import idempotency_key
from .idempotency import find_replay, record_key, replayed_resource_gone

router = APIRouter(prefix="/api/integrations", tags=["integrations"])

PROVIDERS = ("google", "strava", "garmin")
OAUTH_PROVIDERS = ("google", "strava")
STATE_TTL = timedelta(minutes=10)


def _account_out(account: IntegrationAccount) -> IntegrationAccountOut:
    return IntegrationAccountOut(
        id=account.id,
        provider=account.provider,
        external_account=account.external_account,
        scopes=account.scopes,
        status=account.status,
        last_sync_at=account.last_sync_at,
        created_at=account.created_at,
    )


@router.get("", response_model=List[IntegrationProviderOut])
def list_integrations(
    actor: Actor = Depends(get_actor),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> List[IntegrationProviderOut]:
    accounts = {
        account.provider: account
        for account in db.scalars(
            select(IntegrationAccount).where(
                IntegrationAccount.workspace_id == actor.workspace_id
            )
        )
    }
    return [
        IntegrationProviderOut(
            provider=provider,
            configured=settings.provider_configured(provider),
            account=_account_out(accounts[provider]) if provider in accounts else None,
        )
        for provider in PROVIDERS
    ]


@router.post("/{provider}/connect", response_model=IntegrationConnectOut)
def connect_integration(
    provider: str,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(require_owner),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> IntegrationConnectOut:
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown provider")
    if provider not in OAUTH_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail="Garmin connects with account credentials, not OAuth",
        )
    if not settings.provider_configured(provider):
        raise HTTPException(
            status_code=400,
            detail=(
                "Provider is not configured. Set the client credentials and "
                "INTEGRATIONS_ENCRYPTION_KEY in .env."
            ),
        )
    state = secrets.token_urlsafe(32)[:64]
    db.execute(
        delete(OAuthState).where(
            OAuthState.workspace_id == actor.workspace_id,
            OAuthState.created_at < utcnow() - STATE_TTL,
        )
    )
    db.add(
        OAuthState(
            workspace_id=actor.workspace_id,
            user_id=actor.user_id,
            provider=provider,
            state=state,
        )
    )
    db.commit()
    return IntegrationConnectOut(
        authorize_url=connector_base.authorize_url(provider, state, settings)
    )


@router.post("/garmin/credentials", response_model=IntegrationAccountOut, status_code=201)
def connect_garmin(
    payload: GarminCredentialsIn,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(require_owner),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> IntegrationAccountOut:
    if not settings.integrations_ready:
        raise HTTPException(
            status_code=400,
            detail="Set INTEGRATIONS_ENCRYPTION_KEY in .env to enable integrations.",
        )
    replay = find_replay(
        db,
        workspace_id=actor.workspace_id,
        operation="integration.garmin",
        key=key,
    )
    if replay:
        existing = db.get(IntegrationAccount, replay.resource_id)
        if existing is None or existing.workspace_id != actor.workspace_id:
            raise replayed_resource_gone()
        return _account_out(existing)
    try:
        client = garmin_connector.login_with_credentials(
            payload.email, payload.password
        )
    except ConnectorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    token = garmin_connector.session_token(client)
    account = db.scalar(
        select(IntegrationAccount).where(
            IntegrationAccount.workspace_id == actor.workspace_id,
            IntegrationAccount.provider == "garmin",
            IntegrationAccount.external_account == payload.email,
        )
    )
    if account is None:
        account = IntegrationAccount(
            workspace_id=actor.workspace_id,
            user_id=actor.user_id,
            provider="garmin",
            external_account=payload.email,
            scopes="garmin-connect (unofficial)",
        )
        db.add(account)
    # The password is never stored; only the garth session token, encrypted.
    account.refresh_token_enc = encrypt_secret(token, settings)
    account.access_token_enc = ""
    account.status = "connected"
    db.flush()
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="integration.garmin",
        key=key,
        resource_id=account.id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="integration.connected",
        resource_type="integration_account",
        resource_id=account.id,
        detail={"provider": "garmin", "account": payload.email},
    )
    db.commit()
    return _account_out(account)


@router.get("/{provider}/callback")
def integration_callback(
    provider: str,
    code: str = "",
    state: str = "",
    error: str = "",
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    web_origin = settings.allowed_web_origins[0] if settings.allowed_web_origins else ""
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown provider")
    record = db.scalar(
        select(OAuthState).where(
            OAuthState.provider == provider,
            OAuthState.state == state,
        )
    )
    if record is None or record.created_at < utcnow() - STATE_TTL:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")
    db.delete(record)
    if error or not code:
        db.commit()
        return RedirectResponse(url=f"{web_origin}/?integration_error={provider}")
    payload = connector_base.exchange_code(provider, code, settings)

    external_account = ""
    scopes = str(payload.get("scope", ""))
    if provider == "google":
        access_token = str(payload.get("access_token", ""))
        try:
            external_account = gmail_connector.fetch_profile(access_token)
        except Exception:
            external_account = ""
        scopes = scopes or connector_base.GMAIL_SCOPE
    elif provider == "strava":
        athlete = payload.get("athlete") or {}
        external_account = str(athlete.get("username") or athlete.get("id") or "")
        scopes = scopes or "read,activity:read_all"

    account = db.scalar(
        select(IntegrationAccount).where(
            IntegrationAccount.workspace_id == record.workspace_id,
            IntegrationAccount.provider == provider,
            IntegrationAccount.external_account == external_account,
        )
    )
    if account is None:
        account = IntegrationAccount(
            workspace_id=record.workspace_id,
            user_id=record.user_id,
            provider=provider,
            external_account=external_account,
            scopes=scopes,
        )
        db.add(account)
    connector_base.store_tokens(account, payload, settings)
    db.flush()
    record_audit(
        db,
        workspace_id=record.workspace_id,
        actor_id=record.user_id,
        action="integration.connected",
        resource_type="integration_account",
        resource_id=account.id,
        detail={"provider": provider, "account": external_account},
    )
    db.commit()
    return RedirectResponse(url=f"{web_origin}/?connected={provider}")


@router.delete("/{account_id}", status_code=204)
def disconnect_integration(
    account_id: str,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
) -> None:
    replay = find_replay(
        db,
        workspace_id=actor.workspace_id,
        operation="integration.disconnect",
        key=key,
    )
    if replay:
        return
    account = db.scalar(
        select(IntegrationAccount).where(
            IntegrationAccount.id == account_id,
            IntegrationAccount.workspace_id == actor.workspace_id,
        )
    )
    if account is None:
        raise HTTPException(status_code=404, detail="Integration not found")
    db.execute(delete(SyncJob).where(SyncJob.account_id == account.id))
    db.delete(account)
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="integration.disconnect",
        key=key,
        resource_id=account_id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="integration.disconnected",
        resource_type="integration_account",
        resource_id=account_id,
        detail={"provider": account.provider},
    )
    db.commit()


@router.post("/{account_id}/sync", response_model=SyncJobOut, status_code=202)
def sync_integration(
    account_id: str,
    background_tasks: BackgroundTasks,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> SyncJobOut:
    replay = find_replay(
        db,
        workspace_id=actor.workspace_id,
        operation="integration.sync",
        key=key,
    )
    if replay:
        job = db.get(SyncJob, replay.resource_id)
        if job is None or job.workspace_id != actor.workspace_id:
            raise replayed_resource_gone()
        return _job_out(job)
    account = db.scalar(
        select(IntegrationAccount).where(
            IntegrationAccount.id == account_id,
            IntegrationAccount.workspace_id == actor.workspace_id,
        )
    )
    if account is None:
        raise HTTPException(status_code=404, detail="Integration not found")
    running = db.scalar(
        select(SyncJob).where(
            SyncJob.account_id == account.id,
            SyncJob.status.in_(("queued", "running")),
        )
    )
    if running:
        raise HTTPException(status_code=409, detail="A sync is already in progress")
    job = SyncJob(
        workspace_id=actor.workspace_id,
        account_id=account.id,
        connector=CONNECTOR_FOR_PROVIDER[account.provider],
    )
    db.add(job)
    db.flush()
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="integration.sync",
        key=key,
        resource_id=job.id,
    )
    db.commit()
    background_tasks.add_task(run_sync_job, job.id)
    return _job_out(job)


def _job_out(job: SyncJob) -> SyncJobOut:
    stats: dict = {}
    try:
        stats = json.loads(job.stats_json or "{}")
    except ValueError:
        stats = {}
    return SyncJobOut(
        id=job.id,
        connector=job.connector,
        status=job.status,
        stats=stats,
        error=job.error,
        started_at=job.started_at,
        finished_at=job.finished_at,
        created_at=job.created_at,
    )


@router.get("/{account_id}/jobs", response_model=List[SyncJobOut])
def list_sync_jobs(
    account_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[SyncJobOut]:
    account = db.scalar(
        select(IntegrationAccount).where(
            IntegrationAccount.id == account_id,
            IntegrationAccount.workspace_id == actor.workspace_id,
        )
    )
    if account is None:
        raise HTTPException(status_code=404, detail="Integration not found")
    jobs = db.scalars(
        select(SyncJob)
        .where(SyncJob.account_id == account.id)
        .order_by(SyncJob.created_at.desc())
        .limit(20)
    )
    return [_job_out(job) for job in jobs]
