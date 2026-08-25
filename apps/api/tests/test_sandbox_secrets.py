"""The sandbox secrets subsystem — the "connect stuff" seam.

Two layers get their own assertions. The service layer is where the three rules
live (encrypted at rest, names cannot shadow the policy env, one decrypt path),
and those are cheapest to pin directly against `SessionLocal`. The API layer is
where the workspace's owner/member split is enforced — read is a member act,
write is an owner act — and where the invariant that *no route returns a value*
has to hold on the wire, not just in a docstring.
"""
from __future__ import annotations

import uuid
from typing import Iterator

import pytest
from conftest import (
    TEST_BASE_URL,
    Identity,
    authenticate,
    create_identity,
    issue_session,
)
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import Membership, User
from app.services.sandbox import secrets as secrets_service
from app.services.sandbox.secrets import SecretError


@pytest.fixture()
def encryption_settings():
    """Configure the integrations key the way `test_integrations` does — the app
    ships without one, and the service refuses to store a secret until it has one.
    Saved and restored so a keyed test does not bleed into a keyless one."""
    settings = get_settings()
    original = settings.integrations_encryption_key
    settings.integrations_encryption_key = SecretStr(Fernet.generate_key().decode())
    yield settings
    settings.integrations_encryption_key = original


@pytest.fixture()
def db() -> Iterator[SessionLocal]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# --- service layer: name validation -------------------------------------


@pytest.mark.parametrize("good", ["STRIPE_API_KEY", "A", "OPENAI_KEY_2", "X_Y_Z"])
def test_validate_name_accepts_a_plain_env_var(good: str) -> None:
    assert secrets_service.validate_name(good) == good


@pytest.mark.parametrize(
    "bad",
    ["lowercase", "1LEADING_DIGIT", "HAS SPACE", "HAS-DASH", "", "HAS.DOT", "wîth_unicode"],
)
def test_validate_name_rejects_anything_that_is_not_an_env_var(bad: str) -> None:
    with pytest.raises(SecretError):
        secrets_service.validate_name(bad)


@pytest.mark.parametrize("reserved", ["GRAIN_SANDBOX", "GRAIN_ANYTHING", "MPLBACKEND"])
def test_validate_name_refuses_names_the_policy_env_owns(reserved: str) -> None:
    """A secret that could set a policy key would let prompt-injected code be told
    the wrong thing about its own sandbox. The create path refuses it; so does this."""
    with pytest.raises(SecretError, match="reserved"):
        secrets_service.validate_name(reserved)


def test_no_network_is_reserved_even_though_it_only_exists_under_none() -> None:
    """`NO_NETWORK` is set by the policy env only under the default `none` policy,
    so a set built from just `open` would miss it. The reserved set spans both."""
    with pytest.raises(SecretError, match="reserved"):
        secrets_service.validate_name("NO_NETWORK")


@pytest.mark.parametrize(
    "dangerous",
    [
        "PATH",
        "PYTHONPATH",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES",
        # The prefix rule, not enumeration: these dynamic-loader fallbacks are
        # exactly what an exact-match list quietly misses.
        "DYLD_FALLBACK_LIBRARY_PATH",
        "DYLD_FRAMEWORK_PATH",
        "LD_AUDIT",
        "GCONV_PATH",
    ],
)
def test_validate_name_refuses_process_steering_env_names(dangerous: str) -> None:
    """These are not policy keys, so the policy-derived reserved set would let them
    through — but a run's env is `{**base, **secrets}`, so a secret named `PATH` or
    `LD_PRELOAD` *wins* the merge and can redirect which binary or shared object the
    interpreter loads. That is code execution wearing a credential's clothes, so the
    name is refused outright — the loader families by `LD_`/`DYLD_` prefix so a
    fallback variant cannot slip past an exact-match list."""
    with pytest.raises(SecretError, match="reserved"):
        secrets_service.validate_name(dangerous)


# --- service layer: storage requires encryption -------------------------


def test_set_secret_without_an_encryption_key_is_a_secret_error(db: SessionLocal) -> None:
    """No key, no encrypted storage — the same failure the OAuth connectors
    surface, raised as `SecretError` so the route turns it into a 400."""
    settings = get_settings()
    original = settings.integrations_encryption_key
    settings.integrations_encryption_key = None  # type: ignore[assignment]
    try:
        with pytest.raises(SecretError, match="INTEGRATIONS_ENCRYPTION_KEY"):
            secrets_service.set_secret(
                db,
                workspace_id="w-none",
                user_id="u1",
                name="STRIPE_API_KEY",
                value="sk_live_1",
                settings=settings,
            )
    finally:
        settings.integrations_encryption_key = original


# --- service layer: round-trip and the one decrypt path -----------------


def test_a_stored_secret_decrypts_back_through_secret_env(
    db: SessionLocal, encryption_settings
) -> None:
    """The whole point: what `set_secret` stored is exactly what `secret_env`
    hands `ensure_session` to fold into a new machine's environment."""
    ws = f"ws-{uuid.uuid4().hex}"
    secrets_service.set_secret(
        db,
        workspace_id=ws,
        user_id="u1",
        name="STRIPE_API_KEY",
        value="sk_live_round_trip",
        settings=encryption_settings,
    )
    env = secrets_service.secret_env(db, workspace_id=ws, settings=encryption_settings)
    assert env == {"STRIPE_API_KEY": "sk_live_round_trip"}


def test_the_value_is_not_stored_in_the_clear(
    db: SessionLocal, encryption_settings
) -> None:
    ws = f"ws-{uuid.uuid4().hex}"
    row = secrets_service.set_secret(
        db,
        workspace_id=ws,
        user_id="u1",
        name="TOKEN",
        value="super-secret-value",
        settings=encryption_settings,
    )
    assert "super-secret-value" not in row.value_enc


def test_set_secret_is_idempotent_by_name_and_rotates_the_value(
    db: SessionLocal, encryption_settings
) -> None:
    ws = f"ws-{uuid.uuid4().hex}"
    secrets_service.set_secret(
        db, workspace_id=ws, user_id="u1", name="TOKEN", value="v1",
        settings=encryption_settings,
    )
    secrets_service.set_secret(
        db, workspace_id=ws, user_id="u2", name="TOKEN", value="v2",
        settings=encryption_settings,
    )
    rows = secrets_service.list_secrets(db, workspace_id=ws)
    assert [r.name for r in rows] == ["TOKEN"]  # replaced, not duplicated
    env = secrets_service.secret_env(db, workspace_id=ws, settings=encryption_settings)
    assert env["TOKEN"] == "v2"


def test_secret_env_skips_a_row_it_cannot_decrypt(
    db: SessionLocal, encryption_settings, caplog
) -> None:
    """A key rotated after a secret was stored must degrade to "that one
    credential is missing", not "no session can be created". The bad row is
    skipped; the good one still arrives — and the skip is logged by name (never
    by ciphertext) so a silently-absent credential is diagnosable."""
    ws = f"ws-{uuid.uuid4().hex}"
    secrets_service.set_secret(
        db, workspace_id=ws, user_id="u1", name="GOOD", value="still-good",
        settings=encryption_settings,
    )
    # A row whose ciphertext will never decrypt under the current key.
    secrets_service.set_secret(
        db, workspace_id=ws, user_id="u1", name="BAD", value="was-good",
        settings=encryption_settings,
    )
    bad = secrets_service.list_secrets(db, workspace_id=ws)
    bad_row = next(r for r in bad if r.name == "BAD")
    bad_row.value_enc = "not-valid-fernet-ciphertext"
    db.commit()

    with caplog.at_level("WARNING"):
        env = secrets_service.secret_env(db, workspace_id=ws, settings=encryption_settings)
    assert env == {"GOOD": "still-good"}
    assert "BAD" in caplog.text and "did not decrypt" in caplog.text
    assert "not-valid-fernet-ciphertext" not in caplog.text  # never the value


def test_set_secret_survives_a_concurrent_first_write_of_the_same_name(
    db: SessionLocal, encryption_settings, monkeypatch
) -> None:
    """Two PUTs for a brand-new name can both see "no row yet" and both try to
    insert; the unique (workspace_id, name) constraint makes the second insert
    raise IntegrityError. The loser must recover to an update (last writer wins)
    rather than surfacing a 500. Simulated by hiding the row from the pre-insert
    lookup once, so the insert path runs against a row that already exists."""
    ws = f"ws-{uuid.uuid4().hex}"
    secrets_service.set_secret(
        db, workspace_id=ws, user_id="u1", name="TOKEN", value="v1",
        settings=encryption_settings,
    )

    real_find = secrets_service._find
    calls = {"n": 0}

    def blind_once(*args, **kwargs):
        # First lookup pretends the row is absent (the racing insert's view);
        # later lookups (the recovery re-select) tell the truth.
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_find(*args, **kwargs)

    monkeypatch.setattr(secrets_service, "_find", blind_once)
    row = secrets_service.set_secret(
        db, workspace_id=ws, user_id="u2", name="TOKEN", value="v2",
        settings=encryption_settings,
    )
    assert row.name == "TOKEN"
    rows = secrets_service.list_secrets(db, workspace_id=ws)
    assert [r.name for r in rows] == ["TOKEN"]  # no duplicate row
    env = secrets_service.secret_env(db, workspace_id=ws, settings=encryption_settings)
    assert env["TOKEN"] == "v2"  # the racing writer's value won, no 500


def test_delete_secret_reports_whether_one_was_there(
    db: SessionLocal, encryption_settings
) -> None:
    ws = f"ws-{uuid.uuid4().hex}"
    secrets_service.set_secret(
        db, workspace_id=ws, user_id="u1", name="TOKEN", value="v",
        settings=encryption_settings,
    )
    assert secrets_service.delete_secret(db, workspace_id=ws, name="TOKEN") is True
    assert secrets_service.delete_secret(db, workspace_id=ws, name="TOKEN") is False


# --- API layer ----------------------------------------------------------


def _member_of(workspace_id: str) -> TestClient:
    """A second person in the same workspace, holding a real session, role=member."""
    session = SessionLocal()
    try:
        user = User(email=f"{uuid.uuid4().hex}@example.com", name="Member")
        session.add(user)
        session.flush()
        session.add(
            Membership(workspace_id=workspace_id, user_id=user.id, role="member")
        )
        session.commit()
        user_id = user.id
    finally:
        session.close()
    token, csrf_token = issue_session(user_id)
    client = TestClient(app, base_url=TEST_BASE_URL)
    return authenticate(
        client,
        Identity(
            user_id=user_id,
            workspace_id=workspace_id,
            token=token,
            csrf_token=csrf_token,
        ),
    )


@pytest.fixture()
def owner_client() -> Iterator[TestClient]:
    identity = create_identity()
    client = TestClient(app, base_url=TEST_BASE_URL)
    authenticate(client, identity)
    client.identity = identity  # type: ignore[attr-defined]
    yield client


def test_owner_can_set_list_and_delete_a_secret(
    owner_client: TestClient, encryption_settings
) -> None:
    put = owner_client.put(
        "/api/sandbox/secrets", json={"name": "STRIPE_API_KEY", "value": "sk_live_api"}
    )
    assert put.status_code == 201, put.text
    body = put.json()
    assert body["name"] == "STRIPE_API_KEY"
    # The response describes the secret but never carries the value.
    assert "value" not in body

    listing = owner_client.get("/api/sandbox/secrets")
    assert listing.status_code == 200
    names = [row["name"] for row in listing.json()]
    assert "STRIPE_API_KEY" in names
    assert all("value" not in row for row in listing.json())

    deleted = owner_client.delete("/api/sandbox/secrets/STRIPE_API_KEY")
    assert deleted.status_code == 204
    assert owner_client.delete("/api/sandbox/secrets/STRIPE_API_KEY").status_code == 404


def test_a_bad_name_is_a_400_from_the_write_route(
    owner_client: TestClient, encryption_settings
) -> None:
    resp = owner_client.put(
        "/api/sandbox/secrets", json={"name": "GRAIN_SANDBOX", "value": "x"}
    )
    assert resp.status_code == 400
    assert "reserved" in resp.json()["detail"]


def test_a_member_may_read_but_not_write_or_delete(
    owner_client: TestClient, encryption_settings
) -> None:
    owner_client.put(
        "/api/sandbox/secrets", json={"name": "SHARED_TOKEN", "value": "v"}
    )
    member = _member_of(owner_client.identity.workspace_id)  # type: ignore[attr-defined]

    # A member's sandbox code shares the machine, so a member can see the names.
    listing = member.get("/api/sandbox/secrets")
    assert listing.status_code == 200
    assert "SHARED_TOKEN" in [row["name"] for row in listing.json()]

    # But adding or removing a shared credential is an owner act.
    assert (
        member.put(
            "/api/sandbox/secrets", json={"name": "OWN_TOKEN", "value": "v"}
        ).status_code
        == 403
    )
    assert member.delete("/api/sandbox/secrets/SHARED_TOKEN").status_code == 403
