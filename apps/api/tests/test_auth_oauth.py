"""Google login: state binding, subject-not-email linking, session issuance."""
from __future__ import annotations

import uuid

import httpx
import pytest
from conftest import TEST_BASE_URL
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import Membership, User, UserIdentity, Workspace
from app.services.auth import oauth

SUBJECT = "google-subject-"


@pytest.fixture
def google(monkeypatch):
    """A Google that answers offline, plus login credentials to talk to it."""
    settings = get_settings()
    monkeypatch.setattr(settings, "google_login_client_id", "login-client-id")
    monkeypatch.setattr(
        settings, "google_login_client_secret", _secret("login-client-secret")
    )
    monkeypatch.setattr(settings, "google_login_redirect_uri", "")

    profiles: dict = {"sub": SUBJECT + uuid.uuid4().hex[:8], "email": "", "verified": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "an-access-token"})
        assert request.headers["authorization"] == "Bearer an-access-token"
        return httpx.Response(
            200,
            json={
                "sub": profiles["sub"],
                "email": profiles["email"],
                "email_verified": profiles["verified"],
                "name": "Federated Person",
            },
        )

    monkeypatch.setattr(oauth, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    yield profiles
    monkeypatch.setattr(oauth, "HTTP_TRANSPORT", None)


def _secret(value: str):
    from pydantic import SecretStr

    return SecretStr(value)


def start_flow(client: TestClient) -> str:
    response = client.get("/api/auth/google/start", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"].startswith(oauth.GOOGLE_AUTH_URL)
    return client.cookies[oauth.STATE_COOKIE_NAME]


def test_start_requires_configuration(anonymous_client):
    assert anonymous_client.get("/api/auth/google/start").status_code == 503


def test_start_asks_only_for_identity_scopes(google, anonymous_client):
    response = anonymous_client.get("/api/auth/google/start", follow_redirects=False)
    location = response.headers["location"]
    assert "scope=openid+email+profile" in location
    # The Gmail integration's scope must never ride along on a login.
    assert "gmail" not in location
    assert "login-client-id" in location


def test_callback_rejects_a_mismatched_state(google, anonymous_client):
    start_flow(anonymous_client)
    response = anonymous_client.get(
        "/api/auth/google/callback?code=abc&state=not-the-state",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "auth_error=state" in response.headers["location"]
    assert get_settings().session_cookie_name not in response.cookies


def test_callback_rejects_a_missing_state_cookie(google, anonymous_client):
    # An attacker can put a code and a state in a URL; what they cannot do is
    # plant the matching cookie in the victim's browser.
    response = anonymous_client.get(
        "/api/auth/google/callback?code=abc&state=some-state", follow_redirects=False
    )
    assert "auth_error=state" in response.headers["location"]


def test_callback_rejects_a_non_ascii_state_without_crashing(google, anonymous_client):
    """Regression: `state=%C3%A9` used to raise TypeError out of the route.

    ``secrets.compare_digest`` refuses a str with non-ASCII characters, and the
    state is a query parameter the attacker writes — so the one input the check
    exists to reject was also the one that turned it into a 500. The flow must
    fail the way every other bad state fails.
    """
    start_flow(anonymous_client)
    for bad_state in ("%C3%A9", "%E2%82%AC" * 4, "%F0%9F%92%A9"):
        response = anonymous_client.get(
            f"/api/auth/google/callback?code=abc&state={bad_state}",
            follow_redirects=False,
        )
        assert response.status_code == 303, bad_state
        assert "auth_error=state" in response.headers["location"], bad_state
        assert get_settings().session_cookie_name not in response.cookies
    assert anonymous_client.get("/api/auth/me").status_code == 401


def test_callback_creates_an_account_and_a_session(google, anonymous_client):
    google["email"] = f"federated-{uuid.uuid4().hex[:8]}@example.com"
    state = start_flow(anonymous_client)
    response = anonymous_client.get(
        f"/api/auth/google/callback?code=abc&state={state}", follow_redirects=False
    )
    assert response.status_code == 303
    assert get_settings().session_cookie_name in response.cookies
    # The cookie works: the browser is signed in.
    me = anonymous_client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user_email"] == google["email"]
    assert me.json()["email_verified"] is True

    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.email == google["email"]))
        assert user is not None
        # No password, and verify_password refuses a null hash, so this account
        # cannot be reached with a guess.
        assert user.password_hash is None
        identity = db.scalar(
            select(UserIdentity).where(UserIdentity.user_id == user.id)
        )
        assert identity is not None and identity.subject == google["sub"]
        membership = db.scalar(select(Membership).where(Membership.user_id == user.id))
        assert membership is not None and membership.role == "owner"
    finally:
        db.close()


def test_identity_is_keyed_on_the_subject_not_the_email(google, anonymous_client):
    original = f"federated-{uuid.uuid4().hex[:8]}@example.com"
    google["email"] = original
    state = start_flow(anonymous_client)
    anonymous_client.get(
        f"/api/auth/google/callback?code=abc&state={state}", follow_redirects=False
    )
    db = SessionLocal()
    try:
        user_id = db.scalar(select(User.id).where(User.email == original))
    finally:
        db.close()

    # Same Google account, new address on it. Matching by email would either
    # create a second account or, worse, hand this one to whoever now owns the
    # old address.
    google["email"] = f"renamed-{uuid.uuid4().hex[:8]}@example.com"
    second = TestClient(app, base_url=TEST_BASE_URL)
    state = start_flow(second)
    second.get(
        f"/api/auth/google/callback?code=abc&state={state}", follow_redirects=False
    )
    assert second.get("/api/auth/me").json()["user_id"] == user_id

    db = SessionLocal()
    try:
        assert db.scalar(select(User).where(User.email == google["email"])) is None
        identities = list(
            db.scalars(select(UserIdentity).where(UserIdentity.user_id == user_id))
        )
        assert len(identities) == 1
        assert identities[0].email == google["email"]
    finally:
        db.close()


def test_an_unverified_google_email_cannot_claim_an_existing_account(
    google, anonymous_client
):
    email = f"claimed-{uuid.uuid4().hex[:8]}@example.com"
    db = SessionLocal()
    try:
        user = User(email=email, name="Local", password_hash="$argon2id$fake")
        db.add(user)
        db.flush()
        workspace = Workspace(name="Local workspace")
        db.add(workspace)
        db.flush()
        db.add(Membership(workspace_id=workspace.id, user_id=user.id, role="owner"))
        db.commit()
    finally:
        db.close()

    google["email"] = email
    google["verified"] = False
    state = start_flow(anonymous_client)
    response = anonymous_client.get(
        f"/api/auth/google/callback?code=abc&state={state}", follow_redirects=False
    )
    # Refused: an address in a Google profile is not proof of owning it, and
    # auto-linking on it would be account takeover.
    assert "auth_error=account_exists" in response.headers["location"]
    assert anonymous_client.get("/api/auth/me").status_code == 401


def test_linking_needs_both_sides_to_have_verified_the_mailbox(
    google, anonymous_client
):
    from app.clock import utcnow

    email = f"linkable-{uuid.uuid4().hex[:8]}@example.com"
    db = SessionLocal()
    try:
        user = User(
            email=email,
            name="Local",
            password_hash="$argon2id$fake",
            email_verified_at=utcnow(),
        )
        db.add(user)
        db.flush()
        workspace = Workspace(name="Linkable workspace")
        db.add(workspace)
        db.flush()
        db.add(Membership(workspace_id=workspace.id, user_id=user.id, role="owner"))
        db.commit()
        user_id = user.id
    finally:
        db.close()

    google["email"] = email
    google["verified"] = True
    state = start_flow(anonymous_client)
    anonymous_client.get(
        f"/api/auth/google/callback?code=abc&state={state}", follow_redirects=False
    )
    assert anonymous_client.get("/api/auth/me").json()["user_id"] == user_id
    db = SessionLocal()
    try:
        identity = db.scalar(
            select(UserIdentity).where(UserIdentity.user_id == user_id)
        )
        assert identity is not None and identity.subject == google["sub"]
    finally:
        db.close()


def test_a_failed_exchange_does_not_sign_anyone_in(google, anonymous_client, monkeypatch):
    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    state = start_flow(anonymous_client)
    monkeypatch.setattr(oauth, "HTTP_TRANSPORT", httpx.MockTransport(failing))
    response = anonymous_client.get(
        f"/api/auth/google/callback?code=abc&state={state}", follow_redirects=False
    )
    assert "auth_error=exchange" in response.headers["location"]
    assert anonymous_client.get("/api/auth/me").status_code == 401


def test_a_denied_consent_screen_is_handled(google, anonymous_client):
    start_flow(anonymous_client)
    response = anonymous_client.get(
        "/api/auth/google/callback?error=access_denied", follow_redirects=False
    )
    assert "auth_error=denied" in response.headers["location"]


def test_login_and_integration_clients_are_separate(google):
    """Wiring check: the login client id is its own setting.

    GOOGLE_CLIENT_ID belongs to the Gmail data integration. Reusing it here
    would mean a mailbox grant and a login share one consent screen and one
    credential, so revoking either breaks the other.
    """
    settings = get_settings()
    assert settings.google_login_client_id == "login-client-id"
    assert settings.google_client_id != settings.google_login_client_id
    assert oauth.GOOGLE_LOGIN_SCOPE == "openid email profile"
