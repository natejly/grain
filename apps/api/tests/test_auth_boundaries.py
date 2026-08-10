"""The boundary itself: fail-closed identity, CSRF, workspace scope, dev doors.

These are the tests that would have caught the vulnerability this work closed —
a request with no credentials, or with a forged identity header, receiving a
real workspace.
"""
from __future__ import annotations

import uuid

import pytest
from conftest import TEST_BASE_URL, create_identity, issue_session
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import DEV_SEED_USER_ID, DEV_SEED_WORKSPACE_ID, get_actor
from app.clock import utcnow
from app.config import Settings, get_settings
from app.database import SessionLocal
from app.main import app
from app.models import Membership, UserSession, Workspace
from app.services.auth.sessions import hash_token

# Unauthenticated by design: these either start a session or are redeemed with a
# single-use token that is itself the credential. Everything else on the API
# must resolve an identity through get_actor.
PUBLIC_UNSAFE_ROUTES = {
    "/api/auth/signup",
    "/api/auth/login",
    "/api/auth/dev-login",
    "/api/auth/password/reset/request",
    "/api/auth/password/reset/confirm",
    "/api/auth/verify-email",
}


def walk_routes(routes):
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from walk_routes(inner.routes)
        elif getattr(route, "routes", None):
            yield from walk_routes(route.routes)
        else:
            yield route


def dependency_calls(dependant):
    calls = [dependant.call]
    for sub in dependant.dependencies:
        calls.extend(dependency_calls(sub))
    return calls


def test_every_state_changing_route_resolves_an_identity():
    """CSRF and authentication are enforced inside get_actor.

    That is only airtight while every unsafe route actually depends on it, so
    this test is the tripwire: add a POST without an actor and the build fails
    rather than shipping an unauthenticated write.
    """
    unguarded = []
    for route in walk_routes(app.routes):
        methods = getattr(route, "methods", set()) or set()
        if not methods & {"POST", "PUT", "PATCH", "DELETE"}:
            continue
        if not hasattr(route, "dependant"):
            continue
        if route.path in PUBLIC_UNSAFE_ROUTES:
            continue
        if get_actor not in dependency_calls(route.dependant):
            unguarded.append(f"{sorted(methods)} {route.path}")
    assert unguarded == []


def test_a_request_without_a_cookie_is_refused(anonymous_client):
    for path in ("/api/bootstrap", "/api/conversations", "/api/memory", "/api/sources"):
        assert anonymous_client.get(path).status_code == 401, path


def test_identity_headers_are_ignored(anonymous_client):
    """The old door: claim to be the seed owner and see what happens."""
    response = anonymous_client.get(
        "/api/bootstrap",
        headers={
            "X-User-Id": DEV_SEED_USER_ID,
            "X-Workspace-Id": DEV_SEED_WORKSPACE_ID,
        },
    )
    assert response.status_code == 401


def test_a_workspace_header_cannot_reach_a_workspace_you_are_not_in(identity_client):
    outsider = identity_client()
    response = outsider.get(
        "/api/bootstrap", headers={"X-Workspace-Id": DEV_SEED_WORKSPACE_ID}
    )
    assert response.status_code == 403
    # And the caller's own workspace is still reachable by name.
    own = outsider.get(
        "/api/bootstrap", headers={"X-Workspace-Id": outsider.identity.workspace_id}
    )
    assert own.status_code == 200
    assert own.json()["identity"]["workspace_id"] == outsider.identity.workspace_id


def join_workspace(user_id: str, name: str, role: str = "member") -> str:
    """Put `user_id` in a second, brand new workspace; return its id."""
    db = SessionLocal()
    try:
        workspace = Workspace(name=name)
        db.add(workspace)
        db.flush()
        db.add(Membership(workspace_id=workspace.id, user_id=user_id, role=role))
        db.commit()
        return workspace.id
    finally:
        db.close()


def test_the_workspace_list_offers_every_workspace_the_caller_is_in(identity_client):
    caller = identity_client(workspace_name="First")
    second = join_workspace(caller.identity.user_id, "Second", role="member")

    listed = caller.get("/api/auth/workspaces").json()
    assert [item["id"] for item in listed] == [caller.identity.workspace_id, second]
    assert [item["name"] for item in listed] == ["First", "Second"]
    assert [item["role"] for item in listed] == ["owner", "member"]
    # No selection was sent, so the current one is the same workspace
    # `_resolve_workspace` picks by default: the oldest membership.
    assert [item["is_current"] for item in listed] == [True, False]


def test_the_workspace_list_follows_the_selection_header(identity_client):
    caller = identity_client()
    second = join_workspace(caller.identity.user_id, "Second")

    listed = caller.get(
        "/api/auth/workspaces", headers={"X-Workspace-Id": second}
    ).json()
    assert {item["id"]: item["is_current"] for item in listed} == {
        caller.identity.workspace_id: False,
        second: True,
    }


def test_the_workspace_list_never_shows_another_users_workspaces(identity_client):
    """The whole point of the switcher's discovery endpoint failing closed.

    It hands out ids that `X-Workspace-Id` will be believed for, so a list that
    leaked a stranger's workspace would be handing out the one thing the header
    check cannot: a workspace id the caller has no membership row for.
    """
    stranger = identity_client(workspace_name="Stranger's workspace")
    join_workspace(stranger.identity.user_id, "Stranger's second")
    caller = identity_client(workspace_name="Caller's workspace")

    listed = caller.get("/api/auth/workspaces").json()
    assert [item["id"] for item in listed] == [caller.identity.workspace_id]
    assert stranger.identity.workspace_id not in {item["id"] for item in listed}
    # And naming the stranger's workspace outright is still a 403, so the list
    # is the only way to learn an id and it only ever tells the truth.
    denied = caller.get(
        "/api/auth/workspaces",
        headers={"X-Workspace-Id": stranger.identity.workspace_id},
    )
    assert denied.status_code == 403


def test_the_workspace_list_requires_a_session(anonymous_client):
    assert anonymous_client.get("/api/auth/workspaces").status_code == 401


def test_an_unknown_cookie_is_refused(anonymous_client):
    anonymous_client.cookies.set(get_settings().session_cookie_name, "not-a-real-token")
    assert anonymous_client.get("/api/bootstrap").status_code == 401


def test_an_expired_session_is_refused(anonymous_client):
    identity = create_identity()
    db = SessionLocal()
    try:
        session = db.scalar(
            select(UserSession).where(
                UserSession.token_hash == hash_token(identity.token)
            )
        )
        assert session is not None
        session.expires_at = utcnow() - session.expires_at.resolution
        db.commit()
    finally:
        db.close()
    anonymous_client.cookies.set(get_settings().session_cookie_name, identity.token)
    assert anonymous_client.get("/api/bootstrap").status_code == 401


def test_a_revoked_session_is_refused(anonymous_client):
    identity = create_identity()
    anonymous_client.cookies.set(get_settings().session_cookie_name, identity.token)
    assert anonymous_client.get("/api/bootstrap").status_code == 200
    db = SessionLocal()
    try:
        session = db.scalar(
            select(UserSession).where(
                UserSession.token_hash == hash_token(identity.token)
            )
        )
        assert session is not None
        session.revoked_at = utcnow()
        db.commit()
    finally:
        db.close()
    assert anonymous_client.get("/api/bootstrap").status_code == 401


def test_a_user_with_no_membership_gets_no_workspace(anonymous_client):
    from app.models import User

    db = SessionLocal()
    try:
        user = User(email=f"orphan-{uuid.uuid4().hex[:8]}@example.com", name="Orphan")
        db.add(user)
        db.commit()
        user_id = user.id
    finally:
        db.close()
    token, _ = issue_session(user_id)
    anonymous_client.cookies.set(get_settings().session_cookie_name, token)
    # A live session is not authorization: with no membership there is no
    # workspace to act in, and the request stops at 403 rather than defaulting.
    assert anonymous_client.get("/api/bootstrap").status_code == 403


def test_a_disabled_user_cannot_use_a_live_session(anonymous_client):
    from app.models import User

    identity = create_identity()
    db = SessionLocal()
    try:
        user = db.get(User, identity.user_id)
        assert user is not None
        user.status = "disabled"
        db.commit()
    finally:
        db.close()
    anonymous_client.cookies.set(get_settings().session_cookie_name, identity.token)
    assert anonymous_client.get("/api/bootstrap").status_code == 401


def test_unsafe_methods_require_the_csrf_header(identity_client):
    caller = identity_client()
    settings = get_settings()
    body = {"title": "CSRF probe"}
    key = {"Idempotency-Key": "csrf-" + uuid.uuid4().hex}

    # This is the cross-site attack: the browser attaches the SameSite=None
    # cookie by itself, but the attacker's page cannot read or set the header.
    del caller.headers[settings.csrf_header_name]
    assert caller.post("/api/conversations", headers=key, json=body).status_code == 403

    caller.headers[settings.csrf_header_name] = "a-plausible-looking-token"
    assert caller.post("/api/conversations", headers=key, json=body).status_code == 403

    caller.headers[settings.csrf_header_name] = caller.identity.csrf_token
    assert caller.post("/api/conversations", headers=key, json=body).status_code == 201


def test_a_partially_correct_csrf_token_is_refused(identity_client):
    """The comparison must be whole-value, not a prefix or a truncation.

    Found by mutation: replacing ``compare_digest`` with ``startswith`` passed
    the suite, because every wrong token it tried was wrong in the first
    character. A prefix-accepting compare is guessable one byte at a time, so
    the near-misses are the cases worth asserting.
    """
    caller = identity_client()
    settings = get_settings()
    real = caller.identity.csrf_token
    near_misses = (
        real[:-1],  # a prefix
        real[:1],  # a very short prefix
        real + "x",  # a superstring
        real[:-1] + ("a" if real[-1] != "a" else "b"),  # differs in the last byte
    )
    for value in near_misses:
        caller.headers[settings.csrf_header_name] = value
        response = caller.post(
            "/api/conversations",
            headers={"Idempotency-Key": "near-" + uuid.uuid4().hex},
            json={"title": "near miss"},
        )
        assert response.status_code == 403, (value, response.status_code)


def test_a_non_ascii_csrf_header_is_refused_not_crashed(identity_client):
    """Regression: a header of raw latin-1 bytes used to 500 instead of 403.

    ``hmac.compare_digest`` raises TypeError for a *str* holding any non-ASCII
    character, and Starlette decodes header bytes as latin-1 — so a byte like
    0xE9 in the CSRF header reached the comparison as "é" and blew up inside the
    dependency. It still denied the request, but by unhandled exception: a 500
    and a stack trace on a path whose whole job is to say no calmly.
    """
    caller = identity_client()
    settings = get_settings()
    real = caller.identity.csrf_token
    for value in (b"\xe9" + real[1:].encode(), real.encode() + b"\xff", b"\x80\x81"):
        response = caller.post(
            "/api/conversations",
            headers={
                "Idempotency-Key": "nonascii-" + uuid.uuid4().hex,
                settings.csrf_header_name.encode(): value,
            },
            json={"title": "non-ascii csrf"},
        )
        assert response.status_code == 403, (value, response.status_code)


def test_safe_methods_do_not_require_the_csrf_header(identity_client):
    caller = identity_client()
    del caller.headers[get_settings().csrf_header_name]
    assert caller.get("/api/bootstrap").status_code == 200
    assert caller.get("/api/conversations").status_code == 200


def test_logout_revokes_the_session_and_clears_the_cookie(identity_client):
    caller = identity_client()
    assert caller.get("/api/auth/me").status_code == 200
    response = caller.post("/api/auth/logout")
    assert response.status_code == 204
    db = SessionLocal()
    try:
        session = db.scalar(
            select(UserSession).where(
                UserSession.token_hash == hash_token(caller.identity.token)
            )
        )
        assert session is not None and session.revoked_at is not None
    finally:
        db.close()
    # Replaying the old token gets nothing, even though the client still has it.
    replay = TestClient(app, base_url=TEST_BASE_URL)
    replay.cookies.set(get_settings().session_cookie_name, caller.identity.token)
    assert replay.get("/api/auth/me").status_code == 401


def test_logout_without_the_csrf_header_is_refused(identity_client):
    caller = identity_client()
    del caller.headers[get_settings().csrf_header_name]
    assert caller.post("/api/auth/logout").status_code == 403
    assert caller.get("/api/auth/me").status_code == 200


def test_login_rotates_an_existing_session_token():
    """Session fixation: a token that existed before the login must not work
    after it."""
    from app.models import Membership, User, Workspace
    from app.services.auth.passwords import hash_password

    email = f"rotate-{uuid.uuid4().hex[:8]}@example.com"
    password = "rotation-test-passphrase"
    db = SessionLocal()
    try:
        user = User(email=email, name="Rotate", password_hash=hash_password(password))
        db.add(user)
        db.flush()
        workspace = Workspace(name="Rotate workspace")
        db.add(workspace)
        db.flush()
        db.add(Membership(workspace_id=workspace.id, user_id=user.id, role="owner"))
        db.commit()
        user_id = user.id
    finally:
        db.close()

    planted, _ = issue_session(user_id)
    client = TestClient(app, base_url=TEST_BASE_URL)
    client.cookies.set(get_settings().session_cookie_name, planted)
    assert client.get("/api/auth/me").status_code == 200

    response = client.post(
        "/api/auth/login", json={"email": email, "password": password}
    )
    assert response.status_code == 200
    rotated = response.cookies[get_settings().session_cookie_name]
    assert rotated != planted

    stale = TestClient(app, base_url=TEST_BASE_URL)
    stale.cookies.set(get_settings().session_cookie_name, planted)
    assert stale.get("/api/auth/me").status_code == 401


def test_session_tokens_are_stored_only_as_hashes():
    identity = create_identity()
    db = SessionLocal()
    try:
        assert (
            db.scalar(
                select(UserSession).where(UserSession.token_hash == identity.token)
            )
            is None
        )
        assert (
            db.scalar(
                select(UserSession).where(
                    UserSession.token_hash == hash_token(identity.token)
                )
            )
            is not None
        )
    finally:
        db.close()


def _settings_kwargs(**overrides) -> dict:
    # A production-shaped Settings needs the production model provider, since
    # the scripted double has a guard of its own.
    base = {"model_provider": "openai", "openai_api_key": "test-key"}
    base.update(overrides)
    return base


def test_dev_auto_login_cannot_be_enabled_outside_development():
    """The one convenience that mints an identity is fenced at startup.

    Same shape as _guard_model_provider: a misconfigured deployment refuses to
    boot instead of serving every anonymous request as the seed user.
    """
    with pytest.raises(ValueError, match="DEV_AUTO_LOGIN"):
        Settings(**_settings_kwargs(app_env="production", dev_auto_login=True))
    for env in ("development", "test"):
        assert Settings(**_settings_kwargs(app_env=env, dev_auto_login=True)).dev_auto_login


def test_dev_user_cannot_be_enabled_outside_development():
    with pytest.raises(ValueError, match="DEV_USER"):
        Settings(**_settings_kwargs(app_env="production", dev_user="lyn"))
    with pytest.raises(ValueError, match="allowlisted"):
        Settings(**_settings_kwargs(app_env="development", dev_user="nate"))
    settings = Settings(**_settings_kwargs(app_env="development", dev_user="lyn"))
    assert settings.dev_override_handle == "lyn"


def test_dev_login_issues_a_session_for_lyn(anonymous_client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "dev_user", "lyn")

    status = anonymous_client.get("/api/auth/dev-override")
    assert status.status_code == 200
    assert status.json() == {"enabled": True, "handle": "lyn"}

    response = anonymous_client.post("/api/auth/dev-login")
    assert response.status_code == 200
    body = response.json()
    assert body["user_name"] == "Lyn"
    assert body["user_email"] == "lyn@local.dev"
    assert body["csrf_token"]
    assert anonymous_client.get("/api/auth/me").status_code == 200
    assert anonymous_client.get("/api/bootstrap").json()["identity"]["user_name"] == "Lyn"


def test_dev_login_is_404_when_disabled(anonymous_client):
    assert anonymous_client.get("/api/auth/dev-override").json() == {
        "enabled": False,
        "handle": "",
    }
    assert anonymous_client.post("/api/auth/dev-login").status_code == 404


def test_an_insecure_session_cookie_cannot_be_configured_outside_development():
    with pytest.raises(ValueError, match="SESSION_COOKIE_SECURE"):
        Settings(**_settings_kwargs(app_env="production", session_cookie_secure=False))


def test_the_dev_seed_refuses_to_run_outside_development():
    from app.auth import seed_dev_workspace

    production = Settings(**_settings_kwargs(app_env="production"))
    db = SessionLocal()
    try:
        with pytest.raises(RuntimeError, match="development or test"):
            seed_dev_workspace(db, production)
    finally:
        db.close()


def test_the_seeded_user_has_no_password():
    """The seed is data, not a credential: nothing can log in as it."""
    from app.models import User

    db = SessionLocal()
    try:
        user = db.get(User, DEV_SEED_USER_ID)
        assert user is not None
        assert user.password_hash is None
    finally:
        db.close()


def test_dev_auto_login_resolves_the_seed_identity_only_when_enabled(monkeypatch):
    """What the browser suite relies on, and what it costs.

    With the flag off — the default, and the only possibility outside
    development/test — a cookie-less request is refused.
    """
    settings = get_settings()
    client = TestClient(app, base_url=TEST_BASE_URL)
    assert client.get("/api/bootstrap").status_code == 401

    monkeypatch.setattr(settings, "dev_auto_login", True)
    response = client.get("/api/bootstrap")
    assert response.status_code == 200
    assert response.json()["identity"]["workspace_id"] == DEV_SEED_WORKSPACE_ID


def test_the_dev_door_is_for_cookie_less_requests_only(monkeypatch):
    """A bad cookie must never be *upgraded* into the seed identity.

    Found by mutation: making `get_actor` fall back to the dev door when a
    cookie fails to resolve passed the whole suite, because the suite runs with
    the door shut. With it open — which is how `make dev` and the browser suite
    run — that same fallback turns "here is a forged token" into "you are the
    seed owner". The door is only ever for a request carrying no cookie at all.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "dev_auto_login", True)
    client = TestClient(app, base_url=TEST_BASE_URL)
    assert client.get("/api/bootstrap").status_code == 200  # door is open

    for bad in ("not-a-real-token", hash_token("not-a-real-token"), "x" * 43):
        client.cookies.set(settings.session_cookie_name, bad)
        assert client.get("/api/bootstrap").status_code == 401, bad

    # A revoked session is a presented cookie too, not an absent one.
    identity = create_identity()
    db = SessionLocal()
    try:
        session = db.scalar(
            select(UserSession).where(
                UserSession.token_hash == hash_token(identity.token)
            )
        )
        assert session is not None
        session.revoked_at = utcnow()
        db.commit()
    finally:
        db.close()
    client.cookies.set(settings.session_cookie_name, identity.token)
    assert client.get("/api/bootstrap").status_code == 401


def test_the_dev_door_checks_the_environment_itself(monkeypatch):
    """Belt as well as suspenders, and the belt needs a test of its own.

    `Settings._guard_auth` refuses DEV_AUTO_LOGIN outside development/test at
    *construction*, but `get_settings()` is an lru_cache holding one long-lived
    instance whose attributes can be set afterwards — a monkeypatch in a test, a
    stray assignment in a script. `_dev_fallback_actor` therefore re-checks
    `is_dev_env` at call time. Found by mutation: deleting that re-check passed
    the entire suite.
    """
    from app.auth import _dev_fallback_actor

    production = Settings(
        **_settings_kwargs(app_env="production")
    ).model_copy(update={"dev_auto_login": True})
    assert production.dev_auto_login and not production.is_dev_env

    db = SessionLocal()
    try:
        with pytest.raises(HTTPException) as raised:
            _dev_fallback_actor(db, production)
        assert raised.value.status_code == 401
    finally:
        db.close()
