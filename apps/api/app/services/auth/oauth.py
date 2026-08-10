"""Google login: authorization-code flow, separate from the Gmail integration.

Two things here are load-bearing security, not plumbing.

**state.** The callback is a plain GET the attacker can also cause. Without a
state value bound to something only this browser holds, an attacker can start a
flow with *their* Google account and trick a victim into completing it, silently
logging the victim into the attacker's account (or, in the linking direction,
attaching the attacker's identity to the victim's account). The state is stored
in a short-lived, HttpOnly, cross-site cookie on the API's own origin and
compared in constant time with the query parameter on the way back.

**Identity is the subject, never the email.** ``UserIdentity`` keys on Google's
``sub``, which is stable and unique forever. Emails are reassignable inside a
Workspace domain, so matching on one hands a recycled address the previous
holder's account.

The profile is read from Google's userinfo endpoint over a direct TLS call the
browser never touches, so there is no JWT to validate: the transport plus the
client secret already prove the response came from Google. (Reading ``sub`` out
of an unverified ``id_token`` the browser handed us would be the classic hole.)
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import Response

from ...config import Settings

# Tests set this to an httpx.MockTransport so the flow runs offline.
HTTP_TRANSPORT: Optional[httpx.BaseTransport] = None

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
# Identity only. The Gmail integration asks for mailbox scopes with its own
# client; a login must never carry data-access consent along with it.
GOOGLE_LOGIN_SCOPE = "openid email profile"

STATE_COOKIE_NAME = "fieldnote_oauth_state"
STATE_COOKIE_MAX_AGE = 600

PROVIDER_GOOGLE = "google"


class OAuthError(RuntimeError):
    """The federated flow could not be completed."""


@dataclass(frozen=True)
class FederatedProfile:
    """What we are willing to believe about a federated user."""

    provider: str
    subject: str
    email: str
    email_verified: bool
    name: str


def new_state() -> str:
    return secrets.token_urlsafe(32)


def authorize_url(settings: Settings, state: str) -> str:
    if not settings.google_login_ready:
        raise OAuthError("Google login is not configured")
    query = urlencode(
        {
            "client_id": settings.google_login_client_id,
            "redirect_uri": settings.google_login_callback_url,
            "response_type": "code",
            "scope": GOOGLE_LOGIN_SCOPE,
            "state": state,
            # Force the chooser so a shared browser cannot silently reuse the
            # last Google session.
            "prompt": "select_account",
        }
    )
    return f"{GOOGLE_AUTH_URL}?{query}"


def set_state_cookie(response: Response, state: str, settings: Settings) -> None:
    response.set_cookie(
        key=STATE_COOKIE_NAME,
        value=state,
        max_age=STATE_COOKIE_MAX_AGE,
        path="/",
        domain=settings.session_cookie_domain,
        secure=settings.session_cookie_secure,
        httponly=True,
        # The callback arrives as a top-level cross-site navigation from
        # accounts.google.com, and Lax would be sent — but None keeps this
        # cookie's rules identical to the session cookie's, which is one less
        # thing to get wrong when the deployment domains change.
        samesite="none",
    )


def clear_state_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=STATE_COOKIE_NAME,
        path="/",
        domain=settings.session_cookie_domain,
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="none",
    )


def state_matches(cookie_state: Optional[str], query_state: Optional[str]) -> bool:
    """Constant-time compare, as bytes.

    ``compare_digest`` raises TypeError for a str containing any non-ASCII
    character, and ``state`` is a query parameter an attacker writes: a callback
    URL carrying ``state=%C3%A9`` turned the mismatch into an unhandled 500
    rather than the "auth_error=state" redirect. Bytes compare cleanly for every
    input and are just as constant-time.
    """
    if not cookie_state or not query_state:
        return False
    return secrets.compare_digest(
        cookie_state.encode("utf-8"), query_state.encode("utf-8")
    )


def _http_client() -> httpx.Client:
    return httpx.Client(timeout=15.0, transport=HTTP_TRANSPORT)


def fetch_google_profile(code: str, settings: Settings) -> FederatedProfile:
    """Trade the authorization code for the caller's Google identity."""
    if not settings.google_login_ready or settings.google_login_client_secret is None:
        raise OAuthError("Google login is not configured")
    payload = {
        "client_id": settings.google_login_client_id,
        "client_secret": settings.google_login_client_secret.get_secret_value(),
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": settings.google_login_callback_url,
    }
    with _http_client() as client:
        token_response = client.post(GOOGLE_TOKEN_URL, data=payload)
        if token_response.status_code != 200:
            raise OAuthError(f"Token exchange failed ({token_response.status_code})")
        access_token = str(token_response.json().get("access_token") or "")
        if not access_token:
            raise OAuthError("Token response contained no access_token")
        profile_response = client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if profile_response.status_code != 200:
        raise OAuthError(f"Profile lookup failed ({profile_response.status_code})")
    body = profile_response.json()
    subject = str(body.get("sub") or "")
    if not subject:
        raise OAuthError("Profile contained no subject")
    email = str(body.get("email") or "").strip().lower()
    return FederatedProfile(
        provider=PROVIDER_GOOGLE,
        subject=subject,
        email=email,
        email_verified=bool(body.get("email_verified")),
        name=str(body.get("name") or email.split("@")[0] or "New user"),
    )
