"""The Profile pane's writes: the display name, the password, and the style.

The rename edits `users.name` in place — it is already the attribution source
(bootstrap Identity, coworking labels) — so the proof is a round trip through
bootstrap, not a new column. The password change proves the current password
first, refuses federated accounts, enforces the shared policy, and logs every
OTHER session out while the session making the change survives.
"""
from __future__ import annotations

import json

from conftest import TEST_BASE_URL, Identity, authenticate, create_identity, issue_session
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import AuditEvent, User
from app.services import api_tokens as token_service
from app.services.auth.passwords import hash_password

CURRENT = "correct-horse-battery-staple"
NEW = "a-brand-new-passphrase-42"


def client_for(identity: Identity) -> TestClient:
    return authenticate(TestClient(app, base_url=TEST_BASE_URL), identity)


def give_password(user_id: str, password: str = CURRENT) -> None:
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        assert user is not None
        user.password_hash = hash_password(password)
        db.commit()
    finally:
        db.close()


def user_email(user_id: str) -> str:
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        assert user is not None
        return user.email
    finally:
        db.close()


# ---------------------------------------------------------------------------
# The rename


def test_rename_echoes_and_reaches_bootstrap_identity():
    identity = create_identity(name="Before Rename")
    client = client_for(identity)
    response = client.patch("/api/me/profile", json={"name": "  After Rename  "})
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "After Rename"
    assert body["user_id"] == identity.user_id
    boot = client.get("/api/bootstrap")
    assert boot.status_code == 200
    assert boot.json()["identity"]["user_name"] == "After Rename"


def test_a_blank_name_is_refused():
    client = client_for(create_identity())
    assert client.patch("/api/me/profile", json={"name": "   "}).status_code == 422
    assert client.patch("/api/me/profile", json={"name": ""}).status_code == 422


def test_an_email_in_the_body_is_ignored():
    identity = create_identity()
    original = user_email(identity.user_id)
    client = client_for(identity)
    response = client.patch(
        "/api/me/profile",
        json={"name": "Renamed", "email": "smuggled@example.com"},
    )
    assert response.status_code == 200
    # Echoed read-only from the row; the smuggled address changed nothing.
    assert response.json()["email"] == original
    assert user_email(identity.user_id) == original


# ---------------------------------------------------------------------------
# The password change


def test_wrong_current_password_is_a_generic_403():
    identity = create_identity()
    give_password(identity.user_id)
    client = client_for(identity)
    response = client.post(
        "/api/me/password",
        json={"current_password": "not-it-at-all-sorry", "new_password": NEW},
    )
    assert response.status_code == 403
    # Generic on purpose: the refusal must not say which part was wrong.
    assert "current" not in response.json()["detail"].lower()
    assert "wrong" not in response.json()["detail"].lower()


def test_a_policy_failing_new_password_is_a_422_with_the_policy_message():
    identity = create_identity()
    give_password(identity.user_id)
    client = client_for(identity)
    response = client.post(
        "/api/me/password",
        json={"current_password": CURRENT, "new_password": "short"},
    )
    assert response.status_code == 422
    assert "at least" in response.json()["detail"]


def test_a_federated_account_is_refused_before_any_verification():
    # create_identity users have no password hash — the Google-only shape.
    client = client_for(create_identity())
    response = client.post(
        "/api/me/password",
        json={"current_password": "anything-goes-here", "new_password": NEW},
    )
    assert response.status_code == 422


def test_success_swaps_the_credential_and_logs_other_sessions_out():
    identity = create_identity()
    give_password(identity.user_id)
    email = user_email(identity.user_id)
    # A second device, signed in before the change.
    other_token, other_csrf = issue_session(identity.user_id)
    other = client_for(
        Identity(
            user_id=identity.user_id,
            workspace_id=identity.workspace_id,
            token=other_token,
            csrf_token=other_csrf,
        )
    )
    assert other.get("/api/bootstrap").status_code == 200

    client = client_for(identity)
    response = client.post(
        "/api/me/password",
        json={"current_password": CURRENT, "new_password": NEW},
    )
    assert response.status_code == 200

    # The changing session survives; the other device is out.
    assert client.get("/api/bootstrap").status_code == 200
    assert other.get("/api/bootstrap").status_code == 401

    # The old password no longer authenticates; the new one does.
    fresh = TestClient(app, base_url=TEST_BASE_URL)
    stale = fresh.post(
        "/api/auth/login", json={"email": email, "password": CURRENT}
    )
    assert stale.status_code == 401
    good = TestClient(app, base_url=TEST_BASE_URL).post(
        "/api/auth/login", json={"email": email, "password": NEW}
    )
    assert good.status_code == 200

    # Audited with no payload detail: the fact of the change is the record.
    db = SessionLocal()
    try:
        row = db.scalar(
            select(AuditEvent)
            .where(
                AuditEvent.workspace_id == identity.workspace_id,
                AuditEvent.action == "password.changed",
            )
            .order_by(AuditEvent.created_at.desc())
        )
        assert row is not None
        assert json.loads(row.detail_json) == {}
    finally:
        db.close()


def test_password_change_revokes_live_api_tokens_and_says_how_many():
    """The leak-response sweep reaches the OTHER bearer-credential class: a
    `grain_…` token minted under the old password (say, by whoever held it)
    must stop resolving the moment the password rotates — it appears in no
    session list, so nothing else would ever kill it."""
    identity = create_identity()
    give_password(identity.user_id)
    db = SessionLocal()
    try:
        minted = token_service.mint(
            db,
            workspace_id=identity.workspace_id,
            user_id=identity.user_id,
            name="minted before the rotation",
        )
        db.commit()
        secret = minted.secret
        token_id = minted.token.id
    finally:
        db.close()
    db = SessionLocal()
    try:
        assert token_service.resolve(db, secret) is not None
    finally:
        db.close()

    client = client_for(identity)
    response = client.post(
        "/api/me/password",
        json={"current_password": CURRENT, "new_password": NEW},
    )
    assert response.status_code == 200, response.text
    # The acknowledgement states the token count — no overstated sweep.
    assert "1 API token revoked" in response.json()["detail"]

    db = SessionLocal()
    try:
        assert token_service.resolve(db, secret) is None
        audit = db.scalar(
            select(AuditEvent).where(
                AuditEvent.workspace_id == identity.workspace_id,
                AuditEvent.action == "api_token.revoked",
                AuditEvent.resource_id == token_id,
            )
        )
        assert audit is not None
        assert json.loads(audit.detail_json)["reason"] == "password_changed"
    finally:
        db.close()


def test_password_change_with_no_tokens_keeps_the_plain_acknowledgement():
    identity = create_identity()
    give_password(identity.user_id)
    client = client_for(identity)
    response = client.post(
        "/api/me/password",
        json={"current_password": CURRENT, "new_password": NEW},
    )
    assert response.status_code == 200, response.text
    assert (
        response.json()["detail"]
        == "Password updated. Other sessions were signed out."
    )


# ---------------------------------------------------------------------------
# The response style


def test_a_fixed_preset_does_not_clobber_the_stored_custom_text():
    """The minimal body `{"preset": "concise"}` — what any non-web client
    naturally sends, since the contract marks the text optional — must not
    erase the member's saved prose; flipping back to Custom finds it again."""
    identity = create_identity()
    client = client_for(identity)
    saved = client.put(
        "/api/me/style",
        json={"preset": "custom", "custom_style_text": "Always answer in haiku."},
    )
    assert saved.status_code == 200, saved.text

    switched = client.put("/api/me/style", json={"preset": "concise"})
    assert switched.status_code == 200, switched.text
    assert switched.json()["preset"] == "concise"
    # The stored prose survived the switch, server-side — no client echo
    # required.
    assert switched.json()["custom_style_text"] == "Always answer in haiku."

    back = client.put(
        "/api/me/style",
        json={"preset": "custom", "custom_style_text": "Always answer in haiku."},
    )
    assert back.status_code == 200
    assert back.json()["custom_style_text"] == "Always answer in haiku."


def test_an_explicit_custom_payload_rewrites_the_stored_text():
    identity = create_identity()
    client = client_for(identity)
    assert (
        client.put(
            "/api/me/style",
            json={"preset": "custom", "custom_style_text": "First directive."},
        ).status_code
        == 200
    )
    rewritten = client.put(
        "/api/me/style",
        json={"preset": "custom", "custom_style_text": "Second directive."},
    )
    assert rewritten.status_code == 200
    assert rewritten.json()["custom_style_text"] == "Second directive."
    # And custom-with-nothing-to-say is still refused, not stored.
    refused = client.put(
        "/api/me/style", json={"preset": "custom", "custom_style_text": "   "}
    )
    assert refused.status_code == 422
