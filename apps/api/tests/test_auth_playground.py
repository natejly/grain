"""Anonymous try-it mode: off and boot-guarded by default, and — when enabled —
an ordinary session over a throwaway workspace that is fenced by the same
membership scoping every real tenant passes, with no password and no way out.
"""
from __future__ import annotations

import uuid

import pytest
from conftest import TEST_BASE_URL
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings, get_settings
from app.database import SessionLocal
from app.main import app
from app.models import AuditEvent, Membership, User

PASSWORD = "correct-horse-battery-staple"


def unique_email() -> str:
    return f"user-{uuid.uuid4().hex[:12]}@example.com"


def fresh_client() -> TestClient:
    return TestClient(app, base_url=TEST_BASE_URL)


def _settings_kwargs(**overrides) -> dict:
    # A production-shaped Settings needs the production model provider and a real
    # mail transport, since each dev-only default has a guard of its own. Same
    # base the auth-boundary guard tests use.
    base = {
        "model_provider": "openai",
        "openai_api_key": "test-key",
        "email_sender": "smtp",
        "smtp_host": "smtp.example.com",
        "web_origin": "https://app.example.com",
    }
    base.update(overrides)
    return base


def test_playground_is_disabled_by_default():
    """Off is the fail-safe default: the button hides and the door 404s.

    The test suite runs with ``PLAYGROUND_ENABLED`` unset, which is the same
    posture a production deployment has until an operator opts in.
    """
    client = fresh_client()
    status = client.get("/api/auth/playground")
    assert status.status_code == 200
    assert status.json() == {"enabled": False}
    # A disabled feature is indistinguishable from one that does not exist.
    assert client.post("/api/auth/playground").status_code == 404


def test_playground_config_validator_fences_the_credential_free_session():
    """The boot guard, same shape as _guard_auth / _guard_sandbox.

    Playground is *allowed* in production — it is a real isolated session, not an
    auth bypass — but only when the secure-cookie posture that protects a real
    login also protects it. The one unsafe combination (a credential-free session
    riding an insecure cross-site cookie any visitor can mint) refuses to boot.
    """
    # The belt: production + insecure cookie never constructs at all. _guard_auth
    # runs first and forbids the insecure cookie outright, so the deployment
    # refuses to boot regardless of playground.
    with pytest.raises(ValueError, match="SESSION_COOKIE_SECURE"):
        Settings(
            **_settings_kwargs(
                app_env="production",
                playground_enabled=True,
                session_cookie_secure=False,
            )
        )

    # The suspenders: _guard_playground asserts the same secure-cookie guarantee
    # for *this* feature, so a future relaxation of _guard_auth cannot silently
    # weaken it. It is unreachable through normal construction (the belt fires
    # first), so — like the dev-door re-check test drives _dev_fallback_actor
    # directly — exercise the guard on an instance forced into the unsafe state.
    valid = Settings(**_settings_kwargs(app_env="production", playground_enabled=True))
    forced_insecure = valid.model_copy(update={"session_cookie_secure": False})
    assert not forced_insecure.is_dev_env
    with pytest.raises(ValueError, match="PLAYGROUND_ENABLED"):
        forced_insecure._guard_playground()

    # Production is fine once the cookie is secure — this is a public demo, not a
    # dev relaxation, so unlike DEV_AUTO_LOGIN it is not fenced to is_dev_env.
    assert valid.playground_enabled
    # And in development the insecure cookie is already permitted, so the guard
    # returns early on is_dev_env with nothing to add.
    for env in ("development", "test"):
        assert Settings(
            **_settings_kwargs(
                app_env=env, playground_enabled=True, session_cookie_secure=False
            )
        ).playground_enabled


def test_playground_mints_a_passwordless_owner_session_when_enabled(monkeypatch):
    """Enabled, POST seeds a throwaway account and mints a normal session.

    The guest owns exactly one fresh workspace, has a NULL password hash (so
    nothing can ever password-login to it), and the account creation is audited
    as ``method: playground`` — the same shape signup/dev/google record.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "playground_enabled", True)

    guest = fresh_client()
    assert guest.get("/api/auth/playground").json() == {"enabled": True}

    response = guest.post("/api/auth/playground")
    assert response.status_code == 200
    body = response.json()
    assert body["role"] == "owner"
    assert body["csrf_token"]
    assert body["email_verified"] is False
    assert body["user_email"].startswith("playground-")
    assert body["user_email"].endswith("@playground.local")
    # The session is fully usable, travelling the ordinary cookie -> session path.
    assert guest.get("/api/auth/me").status_code == 200
    assert guest.get("/api/bootstrap").status_code == 200

    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.email == body["user_email"]))
        assert user is not None
        # No credential exists for this account, ever.
        assert user.password_hash is None
        # Exactly one owner membership, in its own workspace.
        memberships = list(
            db.scalars(select(Membership).where(Membership.user_id == user.id))
        )
        assert len(memberships) == 1
        assert memberships[0].role == "owner"
        assert memberships[0].workspace_id == body["workspace_id"]
        # Provenance is recorded.
        audit = db.scalar(
            select(AuditEvent).where(
                AuditEvent.actor_id == user.id,
                AuditEvent.action == "auth.account.created",
            )
        )
        assert audit is not None
        assert '"method":"playground"' in audit.detail_json
    finally:
        db.close()


def test_each_playground_visit_is_a_distinct_isolated_identity(monkeypatch):
    """No shared anonymous account: two visits are two separate throwaways."""
    settings = get_settings()
    monkeypatch.setattr(settings, "playground_enabled", True)

    first = fresh_client().post("/api/auth/playground").json()
    second = fresh_client().post("/api/auth/playground").json()
    assert first["user_id"] != second["user_id"]
    assert first["user_email"] != second["user_email"]
    assert first["workspace_id"] != second["workspace_id"]


def test_a_playground_guest_cannot_reach_another_workspace(monkeypatch, identity_client):
    """The isolation guarantee: a guest is fenced by ordinary membership scoping.

    A real tenant owns a workspace with a private conversation. The guest holds a
    normal session over its own throwaway workspace, so naming the tenant's
    workspace with ``X-Workspace-Id`` is a 403 — the same fence two real tenants
    hit — and the guest's own view never contains the tenant's rows.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "playground_enabled", True)

    victim = identity_client(workspace_name="Private tenant")
    created = victim.post(
        "/api/conversations",
        headers={"Idempotency-Key": "iso-" + uuid.uuid4().hex},
        json={"title": "secret"},
    )
    assert created.status_code == 201
    secret_conversation_id = created.json()["id"]

    guest = fresh_client()
    assert guest.post("/api/auth/playground").status_code == 200

    # Naming the tenant's workspace is refused: the guest has no membership row.
    denied = guest.get(
        "/api/bootstrap", headers={"X-Workspace-Id": victim.identity.workspace_id}
    )
    assert denied.status_code == 403

    # The guest's own workspace is the only one it can see, and it is empty of the
    # tenant's data.
    listed = guest.get("/api/auth/workspaces").json()
    assert [w["id"] for w in listed] != []
    assert victim.identity.workspace_id not in {w["id"] for w in listed}
    own_conversations = guest.get("/api/conversations").json()
    assert secret_conversation_id not in {c["id"] for c in own_conversations}


def test_a_playground_guest_has_no_admin_over_another_workspace(
    monkeypatch, identity_client
):
    """No escalation: owner of its own throwaway is not owner of anyone else's.

    The guest is an owner (of its own fresh workspace), so ``require_owner``
    admits it there — but pointed at a tenant's workspace the membership fence
    denies it before any owner check, so the guest can never administer, promote,
    or read the members of a workspace it does not belong to.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "playground_enabled", True)

    victim = identity_client(workspace_name="Private tenant")
    guest = fresh_client()
    assert guest.post("/api/auth/playground").status_code == 200

    # Admin over the tenant's workspace is a 403 — the guest is not a member.
    stolen = guest.get(
        "/api/admin/members", headers={"X-Workspace-Id": victim.identity.workspace_id}
    )
    assert stolen.status_code == 403
    # Admin over its own workspace works and shows only itself, proving the 403
    # above is the membership fence and not a blanket admin lockout.
    own = guest.get("/api/admin/members")
    assert own.status_code == 200
    assert len(own.json()) == 1


def test_a_playground_guest_cannot_be_promoted_to_a_credentialed_user(monkeypatch):
    """There is no path from the anonymous guest to a real password login.

    ``password_hash`` is NULL and ``verify_password`` refuses a null hash, so no
    guessed or empty password can ever authenticate as the guest — the same
    stance as Google and dev-seed accounts. The throwaway is a dead end.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "playground_enabled", True)

    body = fresh_client().post("/api/auth/playground").json()
    guest_email = body["user_email"]

    for attempt in ("", "password", "None", "null", PASSWORD):
        response = fresh_client().post(
            "/api/auth/login", json={"email": guest_email, "password": attempt}
        )
        # Empty is rejected by the schema (422); everything else by the null-hash
        # comparison (401). Neither ever yields a session.
        assert response.status_code in (401, 422), (attempt, response.status_code)
