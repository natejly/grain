from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, Optional

import httpx
from sqlalchemy.orm import Session

from ...clock import utc_from_timestamp, utcnow
from ...config import Settings, get_settings
from ...models import IntegrationAccount
from ..crypto import decrypt_secret, encrypt_secret

# Tests may set this to an httpx.MockTransport to run connectors offline.
HTTP_TRANSPORT: Optional[httpx.BaseTransport] = None

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
STRAVA_AUTH_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"

TOKEN_URLS = {"google": GOOGLE_TOKEN_URL, "strava": STRAVA_TOKEN_URL}


class ConnectorError(RuntimeError):
    pass


def http_client(timeout: float = 15.0) -> httpx.Client:
    return httpx.Client(timeout=timeout, transport=HTTP_TRANSPORT)


def _client_credentials(provider: str, settings: Settings) -> Dict[str, str]:
    if provider == "google":
        secret = settings.google_client_secret
        client_id = settings.google_client_id
    elif provider == "strava":
        secret = settings.strava_client_secret
        client_id = settings.strava_client_id
    else:
        raise ConnectorError(f"Unknown provider: {provider}")
    if not client_id or secret is None:
        raise ConnectorError(f"Provider {provider} is not configured")
    return {"client_id": client_id, "client_secret": secret.get_secret_value()}


def exchange_code(
    provider: str,
    code: str,
    settings: Optional[Settings] = None,
) -> Dict[str, Any]:
    settings = settings or get_settings()
    data = dict(_client_credentials(provider, settings))
    data.update({"code": code, "grant_type": "authorization_code"})
    if provider == "google":
        data["redirect_uri"] = (
            settings.oauth_redirect_base.rstrip("/") + "/api/integrations/google/callback"
        )
    with http_client() as client:
        response = client.post(TOKEN_URLS[provider], data=data)
    if response.status_code != 200:
        raise ConnectorError(f"Token exchange failed ({response.status_code})")
    return response.json()


def store_tokens(
    account: IntegrationAccount,
    payload: Dict[str, Any],
    settings: Optional[Settings] = None,
) -> None:
    settings = settings or get_settings()
    access_token = str(payload.get("access_token") or "")
    if not access_token:
        raise ConnectorError("Token response contained no access_token")
    account.access_token_enc = encrypt_secret(access_token, settings)
    refresh_token = payload.get("refresh_token")
    if refresh_token:
        account.refresh_token_enc = encrypt_secret(str(refresh_token), settings)
    expires_in = payload.get("expires_in")
    expires_at = payload.get("expires_at")
    if expires_at:
        account.token_expires_at = utc_from_timestamp(int(expires_at))
    elif expires_in:
        account.token_expires_at = utcnow() + timedelta(seconds=int(expires_in))
    account.status = "connected"


def ensure_access_token(
    db: Session,
    account: IntegrationAccount,
    settings: Optional[Settings] = None,
) -> str:
    """Return a valid access token, refreshing (and persisting) when expired."""
    settings = settings or get_settings()
    fresh = (
        account.token_expires_at is None
        or account.token_expires_at > utcnow() + timedelta(seconds=60)
    )
    if fresh and account.access_token_enc:
        return decrypt_secret(account.access_token_enc, settings)
    if not account.refresh_token_enc:
        account.status = "error"
        db.commit()
        raise ConnectorError("Access token expired and no refresh token is stored")
    data = dict(_client_credentials(account.provider, settings))
    data.update(
        {
            "grant_type": "refresh_token",
            "refresh_token": decrypt_secret(account.refresh_token_enc, settings),
        }
    )
    with http_client() as client:
        response = client.post(TOKEN_URLS[account.provider], data=data)
    if response.status_code != 200:
        account.status = "error"
        db.commit()
        raise ConnectorError(f"Token refresh failed ({response.status_code})")
    store_tokens(account, response.json(), settings)
    db.commit()
    return decrypt_secret(account.access_token_enc, settings)


def authorize_url(provider: str, state: str, settings: Optional[Settings] = None) -> str:
    settings = settings or get_settings()
    credentials = _client_credentials(provider, settings)
    redirect = (
        settings.oauth_redirect_base.rstrip("/")
        + f"/api/integrations/{provider}/callback"
    )
    if provider == "google":
        params = httpx.QueryParams(
            client_id=credentials["client_id"],
            redirect_uri=redirect,
            response_type="code",
            scope=GMAIL_SCOPE,
            access_type="offline",
            prompt="consent",
            state=state,
        )
        return f"{GOOGLE_AUTH_URL}?{params}"
    params = httpx.QueryParams(
        client_id=credentials["client_id"],
        redirect_uri=redirect,
        response_type="code",
        scope="read,activity:read_all",
        state=state,
    )
    return f"{STRAVA_AUTH_URL}?{params}"
